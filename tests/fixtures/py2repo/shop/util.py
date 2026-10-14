import re
import urllib2
import urlparse
try:
    import cStringIO as StringIO
except ImportError:
    import StringIO

def slugify(s):
    return re.sub(r'[^a-z0-9]+', '-', s.lower()).strip('-')

def host(url):
    return urlparse.urlparse(url).netloc

def fetch(url):
    return urllib2.urlopen(url).read()
