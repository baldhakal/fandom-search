# coding: utf-8

import re
import os
import sys
import json
import csv
import random
import argparse
import requests
import collections
from time import sleep

import numpy
import pandas as pd
from Levenshtein import distance as lev_distance
from bs4 import BeautifulSoup

import search

import spacy
_SPACY_MODEL = None  # Model to be loaded later, after we know we need it.
_ANN_INDEX = None

try:
    import lextrie
    bing = lextrie.LexTrie.from_plugin('bing')

    try:
        emolex = lextrie.LexTrie.from_plugin('emolex_en')
    except Exception:
        emolex = None

    try:
        liwc = lextrie.LexTrie.from_plugin('liwc')
    except Exception:
        liwc = None
except ImportError:
    bing = None
    emolex = None
    liwc = None


# -----------------------------------------------------------------------------
# HTML TO TXT FUNCTIONS
# ---------------------

def get_fan_work(fan_html_name):
    with open(fan_html_name, encoding='utf8') as fan_in:
        fan_html = BeautifulSoup(fan_in.read(), "lxml")
        fan_txt = fan_html.find(id='workskin')
        if fan_txt is None:
            return ''

    fan_txt = ' '.join(fan_txt.strings)
    fan_txt = re.split(r'Work Text\b([\s:]*)', fan_txt, maxsplit=1)[-1]
    fan_txt = re.split(r'Chapter 1\b([\s:]*)', fan_txt, maxsplit=1)[-1]
    fan_txt = fan_txt.replace('Chapter Text', ' ')
    fan_txt = re.sub(r'\s+', ' ', fan_txt).strip()
    return fan_txt

def convert_dir(io):
    html_dir = io['i']
    out_dir = io['o']

    try:
        os.makedirs(out_dir)
    except Exception:
        pass

    errors = []
    for infile in os.listdir(html_dir):
        base, ext = os.path.splitext(infile)
        outfile = os.path.join(out_dir, base + '.txt')
        infile = os.path.join(html_dir, infile)

        if not os.path.exists(outfile):
            text = get_fan_work(infile)
            if text:
                with open(outfile, 'w', encoding='utf-8') as out:
                    out.write(text)
            else:
                errors.append(infile)

    error_outfile = 'clean-html-errors.txt'
    with open(error_outfile, 'w', encoding='utf-8') as out:
        out.write('The following files were not converted:\n\n')
        for e in errors:
            out.write(e)
            out.write('\n')

# ------------------
# METADATA FUNCTIONS
# ------------------

def select_text(soup_node, selector):
    sel = soup_node.select(selector)
    return sel[0].get_text().strip() if sel else 'AOOO_UNSPECIFIED'
    # "AOOO_UNSPECIFIED" means value not in An Archive of Our Own metadata field

meta_headers = ['FILENAME', 'TITLE', 'AUTHOR', 'SUMMARY', 'NOTES',
                'PUBLICATION_DATE', 'LANGUAGE', 'TAGS']
def get_fan_meta(fan_html_name):
    with open(fan_html_name, encoding='utf8') as fan_in:
        fan_html = BeautifulSoup(fan_in.read(), 'lxml')

    title = select_text(fan_html, '.title.heading')
    author = select_text(fan_html, '.byline.heading')
    summary = select_text(fan_html, '.summary.module')
    notes = select_text(fan_html, '.notes.module')
    date = select_text(fan_html, 'dd.published')
    language = select_text(fan_html, 'dd.language')
    tags = {k.get_text().strip().strip(':'):
            v.get_text(separator='; ').strip().strip('\n; ')
            for k, v in
            zip(fan_html.select('dt.tags'), fan_html.select('dd.tags'))}
    tags = json.dumps(tags)

    path, filename = os.path.split(fan_html_name)

    vals = [filename, title, author, summary, notes,
            date, language, tags]
    return dict(zip(meta_headers, vals))

def collect_meta(io):
    in_dir = io['i']
    out_file = io['o']

    errors = []
    rows = []
    for infile in os.listdir(in_dir):
        infile = os.path.join(in_dir, infile)
        rows.append(get_fan_meta(infile))

    error_outfile = out_file + '-errors.txt'
    with open(error_outfile, 'w', encoding='utf-8') as out:
        out.write('Metadata could not be collected from the following files:\n\n')
        for e in errors:
            out.write(e)
            out.write('\n')

    csv_outfile = out_file + '.csv'
    with open(csv_outfile, 'w', encoding='utf-8') as out:
        wr = csv.DictWriter(out, fieldnames=meta_headers)
        wr.writeheader()
        for row in rows:
            wr.writerow(row)

#----------------
#SCRAPE FUNCTIONS
#----------------
class Logger:
    def __init__(self, logfile='log.txt'):
        self.logfile = logfile

    def log(self, msg, newline=True):
        with open(self.logfile, 'a') as f:
            f.write(msg)
            if newline:
                f.write('\n')

_logger = Logger()
log = _logger.log

_error_id_log = Logger(logfile='error-ids.txt')
log_error_id = _error_id_log.log

def load_error_ids():
    with open(_error_id_log.logfile, 'w+') as ip:
        ids = set(l.strip() for l in ip.readlines())
        return ids

class InlineDisplay:
    def __init__(self):
        self.currlen = 0

    def display(self, s):
        print(s, end=' ')
        sys.stdout.flush()
        self.currlen += len(s) + 1

    def reset(self):
        print('', end='\r')
        print(' ' * self.currlen, end='\r')
        sys.stdout.flush()
        self.currlen = 0

_id = InlineDisplay()
display = _id.display
reset_display = _id.reset

def request_loop(url, timeout=4.0, sleep_base=1.0):
    # We try 20 times. But we double the delay each time,
    # so that we don't get really annoying. Eventually the
    # delay will be more than an hour long, at which point
    # we'll try a few more times, and then give up.

    orig_url = url
    for i in range(20):
        if sleep_base > 7200:  # Only delay up to an hour.
            sleep_base /= 2
            url = '{}#{}'.format(orig_url, random.randrange(1000))
        display('Sleeping for {} seconds;'.format(sleep_base))
        sleep(sleep_base)
        try:
            response = requests.get(url, timeout=timeout)
            response.raise_for_status()
            return response.text
        except requests.exceptions.HTTPError:
            code = response.status_code
            if code >= 400 and code < 500:
                display('Unrecoverable error ({})'.format(code))
                return ''
            else:
                sleep_base *= 2
                display('Recoverable error ({});'.format(code))
        except requests.exceptions.ReadTimeout as exc:
            sleep_base *= 2
            display('Read timed out -- trying again;')
        except requests.exceptions.RequestException as exc:
            sleep_base *= 2
            display('Unexpected error ({}), trying again;\n'.format(exc))
    else:
        return None

def scrape(io):
    search_term = io['search']
    tag = io['tag']
    header = io['url']
    out_dir = io['out']
    end = io['startpage']

    # tag scraping option
    if search_term:
        pp = 1
        safe_search = search_term.replace(' ', '+')
        # an alternative here is to scrape this page and use regex to filter the results:
        # http://archiveofourown.org/media/Movies/fandoms?
        # the canonical filter is used here because the "fandom" filter on the
        # beta tag search is broken as of November 2017
        search_ref = "http://archiveofourown.org/tags/search?utf8=%E2%9C%93&query%5Bname%5D=" + safe_search + "&query%5Btype%5D=&query%5Bcanonical%5D=true&page="
        print('\nTags:')

        tags = ["initialize"]
        while (len(tags)) != 0:
            results_page = requests.get(search_ref + str(pp))
            results_soup = BeautifulSoup(results_page.text, "lxml")
            tags = results_soup(attrs={'href': re.compile('^/tags/[^s]....[^?].*')})

            for x in tags:
                print(x.string)

            pp += 1

    # fan work scraping options
    if header or tag:
        try:
            os.makedirs(out_dir)
        except Exception:
            pass

        os.chdir(out_dir)
        error_works = load_error_ids()

        results = ["initialize"]
        while (len(results)) != 0:
            log('\n\nPAGE ' + str(end))
            print('Page {} '.format(end))

            display('Loading table of contents;')

            if tag:
                mod_header = tag.replace(' ', '%20')
                header = "http://archiveofourown.org/tags/" + mod_header + "/works"

            request_url = header + "?page=" + str(end)
            toc_page = request_loop(request_url)
            if not toc_page:
                err_msg = 'Error loading TOC; aborting.'
                log(err_msg)
                display(err_msg)
                reset_display()
                continue

            toc_page_soup = BeautifulSoup(toc_page, "lxml")
            results = toc_page_soup(attrs={'href': re.compile('^/works/[0-9]+[0-9]$')})

            log('Number of Works on Page {}: {}'.format(end, len(results)))
            log('Page URL: {}'.format(request_url))
            log('Progress: ')

            reset_display()

            for x in results:
                body = str(x).split('"')
                docID = str(body[1]).split('/')[2]
                filename = str(docID) + '.html'

                if os.path.exists(filename):
                    display('Work {} already exists -- skpping;'.format(docID))
                    reset_display()
                    msg = ('skipped existing document {} on '
                           'page {} ({} bytes)')
                    log(msg.format(docID, str(end),
                                   os.path.getsize(filename)))
                elif docID in error_works:
                    display('Work {} is known to cause errors '
                            '-- skipping;'.format(docID))
                    reset_display()
                    msg = ('skipped document {} on page {} '
                           'known to cause errors')
                    log(msg.format(docID, str(end)))

                else:
                    display('Loading work {};'.format(docID))
                    work_request_url = "https://archiveofourown.org/" + body[1] + "?view_adult=true&view_full_work=true"
                    work_page = request_loop(work_request_url)

                    if work_page is None:
                        error_works.add(docID)
                        log_error_id(docID)
                        continue

                    with open(filename, 'w', encoding='utf-8') as html_out:
                        bytes_written = html_out.write(str(work_page))

                    msg = 'reached document {} on page {}, saved {} bytes'
                    log(msg.format(docID, str(end), bytes_written))
                    reset_display()

            reset_display()
            end = end + 1

# ----------------
# matrix functions
# ----------------
class StrictNgramDedupe(object):
    def __init__(self, data_path, ngram_size):
        self.ngram_size = ngram_size

        with open(data_path, encoding='UTF8') as ip:
            rows = list(csv.DictReader(ip))
        self.data = rows
        self.work_matches = collections.defaultdict(list)

        for r in rows:
            self.work_matches[r['FAN_WORK_FILENAME']].append(r)

        # Use n-gram starting index as a unique identifier.
        self.starts_counter = collections.Counter(
            start
            for matches in self.work_matches.values()
            for start in self.to_ngram_starts(self.segment_full(matches))
        )

        filtered_matches = [self.top_ngram(span)
                            for matches in self.work_matches.values()
                            for span in self.segment_full(matches)]

        self.filtered_matches = [ng for ng in filtered_matches
                                 if self.no_better_match(ng)]

    def num_ngrams(self):
        return len(set(int(ng[0]['ORIGINAL_SCRIPT_WORD_INDEX'])
                       for ng in self.filtered_matches))

    def match_to_phrase(self, match):
        return ' '.join(m['ORIGINAL_SCRIPT_WORD'].lower() for m in match)

    def write_match_work_count_matrix(self, out_filename):
        ngrams = {}
        works = set()
        cells = collections.defaultdict(int)
        for m in self.filtered_matches:
            phrase = self.match_to_phrase(m)
            ix = int(m[0]['ORIGINAL_SCRIPT_WORD_INDEX'])
            filename = m[0]['FAN_WORK_FILENAME']

            ngrams[phrase] = ix
            works.add(filename)
            cells[(filename, phrase)] += 1

        ngrams = sorted(ngrams, key=ngrams.get)
        works = sorted(works)
        rows = [[cells[(fn, ng)] for ng in ngrams]
                for fn in works]
        totals = [sum(r[col] for r in rows) for col in range(len(rows[0]))]

        header = ['FILENAME'] + ngrams
        totals = ['(total)'] + totals
        rows = [[fn] + r for fn, r in zip(works, rows)]
        rows = [header, totals] + rows

        with open(out_filename, 'w', encoding='utf-8') as op:
            csv.writer(op).writerows(rows)

    def write_match_sentiment(self, out_filename):
        phrases = {}
        for m in self.filtered_matches:
            phrase = self.match_to_phrase(m)
            ix = int(m[0]['ORIGINAL_SCRIPT_WORD_INDEX'])
            phrases[phrase] = ix
        sorted_phrases = sorted(phrases, key=phrases.get)

        phrase_indices = [phrases[p] for p in sorted_phrases]
        phrases = sorted_phrases

        if emolex:
            emo_count = [emolex.lex_count(p) for p in phrases]
            emo_sent_count = self.project_sentiment_keys(emo_count,
                                                         ['NEGATIVE', 'POSITIVE'])
            emo_emo_count = self.project_sentiment_keys(emo_count,
                                                        ['ANTICIPATION',
                                                         'ANGER',
                                                         'TRUST',
                                                         'SADNESS',
                                                         'DISGUST',
                                                         'SURPRISE',
                                                         'FEAR',
                                                         'JOY'])
        if bing:
            bing_count = [bing.lex_count(p) for p in phrases]
            bing_count = self.project_sentiment_keys(bing_count,
                                                     ['NEGATIVE', 'POSITIVE'])

        if liwc:
            liwc_count = [liwc.lex_count(p) for p in phrases]
            liwc_sent_count = self.project_sentiment_keys(liwc_count,
                                                          ['POSEMO', 'NEGEMO'])
            liwc_other_keys = set(k for ct in liwc_count for k in ct.keys())
            liwc_other_keys -= set(['POSEMO', 'NEGEMO'])
            liwc_other_count = self.project_sentiment_keys(liwc_count,
                                                           liwc_other_keys)

        counts = []
        count_labels = []

        if emolex:
            counts.append(emo_emo_count)
            counts.append(emo_sent_count)
            count_labels.append('NRC_EMOTION_')
            count_labels.append('NRC_SENTIMENT_')

        counts.append(bing_count)
        count_labels.append('BING_SENTIMENT_')

        if liwc:
            counts.append(liwc_sent_count)
            counts.append(liwc_other_count)
            count_labels.append('LIWC_SENTIMENT_')
            count_labels.append('LIWC_ALL_OTHER_')

        rows = self.compile_sentiment_groups(counts, count_labels)

        for r, p, i in zip(rows, phrases, phrase_indices):
            r['{}-GRAM'.format(self.ngram_size)] = p
            r['{}-GRAM_START_INDEX'.format(self.ngram_size)] = i

        fieldnames = sorted(set(k for r in rows for k in r.keys()))
        totals = collections.defaultdict(int)
        skipkeys = ['{}-GRAM_START_INDEX'.format(self.ngram_size),
                    '{}-GRAM'.format(self.ngram_size)]
        totals[skipkeys[0]] = 0
        totals[skipkeys[1]] = '(total)'
        for r in rows:
            for k in r:
                if k not in skipkeys:
                    totals[k] += r[k]
        rows = [totals] + rows

        with open(out_filename, 'w', encoding='utf-8') as op:
            wr = csv.DictWriter(op, fieldnames=fieldnames)
            wr.writeheader()
            wr.writerows(rows)

    def project_sentiment_keys(self, counts, keys):
        counts = [{k: ct.get(k, 0) for k in keys}
                  for ct in counts]
        for ct in counts:
            if sum(ct.values()) == 0:
                ct['UNDETERMINED'] = 1
            else:
                ct['UNDETERMINED'] = 0

        return counts

    def compile_sentiment_groups(self, groups, prefixes):
        new_rows = []
        for group_row in zip(*groups):
            new_row = {}
            for gr, pf in zip(group_row, prefixes):
                for k, v in gr.items():
                    new_row[pf + k] = v
            new_rows.append(new_row)
        return new_rows

    def get_spans(self, indices):
        starts = [0]
        ends = []
        for i in range(1, len(indices)):
            if indices[i] != indices[i - 1] + 1:
                starts.append(i)
                ends.append(i)
        ends.append(len(indices))
        return list(zip(starts, ends))

    def segment_matches(self, matches, key):
        matches = sorted(matches, key=lambda m: int(m[key]))
        indices = [int(m[key]) for m in matches]
        return [[matches[i] for i in range(start, end)]
                for start, end in self.get_spans(indices)]

    def segment_fan_matches(self, matches):
        return self.segment_matches(matches, 'FAN_WORK_WORD_INDEX')

    def segment_orig_matches(self, matches):
        return self.segment_matches(matches, 'ORIGINAL_SCRIPT_WORD_INDEX')

    def segment_full(self, matches):
        return [orig_m
                for fan_m in self.segment_fan_matches(matches)
                for orig_m in self.segment_orig_matches(fan_m)
                if len(orig_m) >= self.ngram_size]

    def to_ngram_starts(self, match_spans):
        return [int(ms[i]['ORIGINAL_SCRIPT_WORD_INDEX'])
                for ms in match_spans
                for i in range(len(ms) - self.ngram_size + 1)]

    def start_count_key(self, span):
        def key(i):
            script_ix = int(span[i]['ORIGINAL_SCRIPT_WORD_INDEX'])
            return self.starts_counter.get(script_ix, 0)
        return key

    def no_better_match(self, ng):
        start = int(ng[0]['ORIGINAL_SCRIPT_WORD_INDEX'])
        best_start = max(range(start - self.ngram_size + 1,
                               start + self.ngram_size),
                         key=self.starts_counter.__getitem__)
        return start == best_start

    def top_ngram(self, span):
        start = max(
            range(len(span) - self.ngram_size + 1),
            key=self.start_count_key(span)
        )
        return span[start: start + self.ngram_size]


def process(inputs):
    ngram_size = inputs['n']
    in_file = inputs['i']
    out_prefix = inputs['m']

    matrix_out = '{}-most-common-perfect-matches-no-overlap-{}-gram-match-matrix.csv'.format(out_prefix, ngram_size)
    sentiment_out = '{}-most-common-perfect-matches-no-overlap-{}-gram-sentiment.csv'.format(out_prefix, ngram_size)

    dd = StrictNgramDedupe(in_file, ngram_size=ngram_size)
    #print(dd.num_ngrams())

    dd.write_match_work_count_matrix(matrix_out)
    dd.write_match_sentiment(sentiment_out)

# -----------------------------------
# data visualization format functions
# -----------------------------------
def project_sentiment_keys_shortform(counts, keys):
        counts = [{k: ct.get(k, 0) for k in keys}
                  for ct in counts]
        for ct in counts:
            if sum(ct.values()) == 0:
                ct['UNDETERMINED'] = 1
            else:
                ct['UNDETERMINED'] = 0
        return counts

def format_data(io):
    fin_data = io['d']
    original_script_markup = fin = io['s']
    fout = io['o']

    matches = pd.read_csv(fin_data)

    name = 'Frequency of Reuse (Exact)'
    positive_match = matches.BEST_COMBINED_DISTANCE <= 0
    matches_thresh = matches.assign(**{name: positive_match})

    thresholds = [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5]
    threshname = ['Frequency of Reuse (0-{})'.format(str(t)) for t in thresholds]
    for thresh, name in zip(thresholds, threshname):
        positive_match = matches.BEST_COMBINED_DISTANCE <= thresh
        matches_thresh = matches_thresh.assign(**{name: positive_match})
    thresholds = [0] + thresholds
    threshname = ['Frequency of Reuse (Exact)'] + threshname

    os_markup_raw = load_markup_script(original_script_markup)
    os_markup_header = os_markup_raw[0]
    os_markup_raw = os_markup_raw[1:]

    lt = emolex # LexTrie.from_plugin('emolex_en')
    emo_terms = ['ANGER',
                 'ANTICIPATION',
                 'DISGUST',
                 'FEAR',
                 'JOY',
                 'SADNESS',
                 'SURPRISE',
                 'TRUST',
                 'NEGATIVE',
                 'POSITIVE']

    pos_terms = ['ANTICIPATION',
                 'JOY',
                 'SURPRISE',
                 'TRUST']

    os_markup_header.extend(emo_terms)
    for r in os_markup_raw:
        emos = lt.get_lex_tags(r[0])
        r.extend(int(t in emos) for t in emo_terms)

    os_markup = pd.DataFrame(os_markup_raw, columns=os_markup_header)
    os_markup.index.name = 'ORIGINAL_SCRIPT_WORD_INDEX'

    match_word_counts = matches_thresh.groupby(
        'ORIGINAL_SCRIPT_WORD_INDEX'
    ).aggregate({
        name: numpy.sum for name in threshname
    })

    match_word_counts = match_word_counts.reindex(
        os_markup.index,
        fill_value=0
    )

    match_word_words = matches_thresh.groupby(
        'ORIGINAL_SCRIPT_WORD_INDEX'
    ).aggregate({
        'ORIGINAL_SCRIPT_WORD': numpy.max,
    })

    match_word_counts = match_word_counts.join(match_word_words)

    match_count = match_word_counts.join(os_markup)
    match_count.to_csv(fout)

def _format_data_sentiment_only(io):
    fin = io['s']
    fout = io['o']

    markup_script = load_markup_script(fin)
    markup_script = markup_script[1:]
    list_script = [[i] + r for i, r in enumerate(markup_script)]

    csv_script = pd.DataFrame(list_script)
    csv_script.columns = ['ORIGINAL_SCRIPT_INDEX',
       'LOWERCASE',
       'SPACY_ORTH_ID',
       'SCENE',
       'CHARACTER']

    bing_count = [bing.lex_count(j[1]) for j in list_script]
    bing_sentiment_keys = ['NEGATIVE', 'POSITIVE']
    bing_count = project_sentiment_keys_shortform(bing_count, bing_sentiment_keys)
    bing_DF = pd.DataFrame(bing_count)

    bing_DF['ORIGINAL_SCRIPT_INDEX'] = csv_script['ORIGINAL_SCRIPT_INDEX']
    out = pd.merge(csv_script, bing_DF, on='ORIGINAL_SCRIPT_INDEX')

    if emolex:
        emo_count = [emolex.lex_count(j[1]) for j in list_script]
        emo_sentiment_keys = ['ANTICIPATION', 'ANGER', 'TRUST', 'SADNESS','DISGUST',
                          'SURPRISE', 'FEAR', 'JOY', 'NEGATIVE', 'POSITIVE']
        emo_count = project_sentiment_keys_shortform(emo_count, emo_sentiment_keys)
        emo_DF = pd.DataFrame(emo_count)
        emo_DF['ORIGINAL_SCRIPT_INDEX'] = csv_script['ORIGINAL_SCRIPT_INDEX']
        out = pd.merge(out, emo_DF, on='ORIGINAL_SCRIPT_INDEX')

    if liwc:
        liwc_count = [liwc.lex_count(j[1]) for j in list_script]

        liwc_sentiment_keys = ['POSEMO', 'NEGEMO']
        liwc_sent_count = project_sentiment_keys_shortform(liwc_count, liwc_sentiment_keys)
        liwc_sent_DF = pd.DataFrame(liwc_sent_count)
        liwc_sent_DF['ORIGINAL_SCRIPT_INDEX'] = csv_script['ORIGINAL_SCRIPT_INDEX']
        out = pd.merge(out, liwc_sent_DF, on='ORIGINAL_SCRIPT_INDEX')

        liwc_other_keys = set(k for ct in liwc_count for k in ct.keys())
        liwc_other_keys -= set(['POSEMO', 'NEGEMO']) #already used these
        liwc_other_count = project_sentiment_keys_shortform(liwc_count, liwc_other_keys)
        liwc_other_DF = pd.DataFrame(liwc_other_count)
        liwc_other_DF['ORIGINAL_SCRIPT_INDEX'] = csv_script['ORIGINAL_SCRIPT_INDEX']
        out = pd.merge(out, liwc_other_DF, on='ORIGINAL_SCRIPT_INDEX')

    out.to_csv(fout + '.csv', index=False)

# -----------------------------------------------------------------------------
# SCRIPT
# ------

if __name__ == '__main__':

    parser = argparse.ArgumentParser(description='process fanworks scraped from Archive of Our Own.')
    subparsers = parser.add_subparsers(help='scrape, clean, getmeta, search, matrix, or format')

    #sub-parsers
    scrape_parser = subparsers.add_parser('scrape', help='find and scrape fanfiction works from Archive of Our Own')
    group = scrape_parser.add_mutually_exclusive_group()
    group.add_argument('-s', '--search', action='store', help="search term to search for a tag to scrape")
    group.add_argument('-t', '--tag', action='store', help="the tag to be scraped")
    group.add_argument('-u', '--url', action='store', help="the full URL of first page to be scraped")
    scrape_parser.add_argument('-o', '--out', action='store', default=os.path.join('.','scraped-html'), help="target directory for scraped html files")
    scrape_parser.add_argument('-p', '--startpage', action='store', default=1, type=int, help="page on which to begin downloading (to resume a previous job)")
    scrape_parser.set_defaults(func=scrape)

    clean_parser = subparsers.add_parser('clean', help='takes a directory of html files and yields a new directory of text files')
    clean_parser.add_argument('i', action='store', help='directory of input html files to clean')
    clean_parser.add_argument('-o', action='store', default='plain-text', help='target directory for output txt files')
    clean_parser.set_defaults(func=convert_dir)

    meta_parser = subparsers.add_parser('getmeta', help='takes a directory of html files and yields a csv file containing metadata')
    meta_parser.add_argument('i', action='store', help='directory of input html files to process')
    meta_parser.add_argument('-o', action='store', default='fan-meta', help='filename for metadata csv file')
    meta_parser.set_defaults(func=collect_meta)

    search_parser = subparsers.add_parser('search', help='compare fanworks with the original script')
    search_parser.add_argument('d', action='store', help='directory of fanwork text files')
    search_parser.add_argument('s', action='store', help='filename for markup version of script')
    search_parser.set_defaults(func=search.analyze)

    matrix_parser = subparsers.add_parser('matrix', help='deduplicates and builds matrix for best n-gram matches')
    matrix_parser.add_argument('i', action='store', help='input csv file')
    matrix_parser.add_argument('m', action = 'store', help='fandom/movie name for output file prefix')
    matrix_parser.add_argument('-n', action='store', default = 6, help='n-gram size, default is 6-grams')
    matrix_parser.set_defaults(func=process)

    data_parser = subparsers.add_parser('format', help='takes a script and outputs a csv with senitment information for each word formatted for javascript visualization')
    data_parser.add_argument('s', action='store', help='filename for markup version of script')
    data_parser.add_argument('d', action='store', help='filename for search output')
    data_parser.add_argument('-o', action='store', default='js-data.csv', help='filename for csv output file of data formatted for visualization')
    data_parser.set_defaults(func=format_data)

    #handle args
    args = parser.parse_args()

    #call function
    if hasattr(args, 'func'):
        _SPACY_MODEL = search.get_spacy_model()
        args.func(vars(args))
    else:
        parser.print_help()

