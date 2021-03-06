#
# misc.py -- Miscellaneous utilities.
#
# Copyright (c) 2007-2009  Christian Hammond
# Copyright (c) 2007-2009  David Trowbridge
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be included
# in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.
# IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY
# CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT,
# TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
# SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#


import logging
import os
import zlib

try:
    import hashlib
    new_md5 = hashlib.md5
except ImportError:
    import md5
    new_md5 = md5.new

try:
    import cPickle as pickle
except ImportError:
    import pickle

try:
    from cStringIO import StringIO
except ImportError:
    from StringIO import StringIO

from django.core.cache import cache
from django.core.urlresolvers import RegexURLPattern
from django.conf import settings
from django.conf.urls import url
from django.contrib.sites.models import Site
from django.db.models.manager import Manager
from django.utils import importlib
from django.views.decorators.cache import never_cache


DEFAULT_EXPIRATION_TIME = 60 * 60 * 24 * 30 # 1 month
CACHE_CHUNK_SIZE = 2**20 - 1024 # almost 1M (memcached's slab limit)

# memcached key size constraint (typically 250, but leave a few bytes for the
# large data handling)
MAX_KEY_SIZE = 240


class MissingChunkError(Exception):
    pass


def _cache_fetch_large_data(cache, key, compress_large_data):
    chunk_count = cache.get(key)
    data = []

    chunk_keys = ['%s-%d' % (key, i) for i in range(int(chunk_count))]
    chunks = cache.get_many(chunk_keys)
    for chunk_key in chunk_keys:
        try:
            data.append(chunks[chunk_key][0])
        except KeyError:
            logging.debug('Cache miss for key %s.' % chunk_key)
            raise MissingChunkError

    data = ''.join(data)

    if compress_large_data:
        data = zlib.decompress(data)

    try:
        unpickler = pickle.Unpickler(StringIO(data))
        data = unpickler.load()
    except Exception, e:
        logging.warning("Unpickle error for cache key %s: %s." % (key, e))
        raise e

    return data


def _cache_store_large_data(cache, key, data, expiration, compress_large_data):
    # We store large data in the cache broken into chunks that are 1M in size.
    # To do this easily, we first pickle the data and compress it with zlib.
    # This gives us a string which can be chunked easily. These are then stored
    # individually in the cache as single-element lists (so the cache backend
    # doesn't try to convert binary data to utf8). The number of chunks needed
    # is stored in the cache under the unadorned key
    file = StringIO()
    pickler = pickle.Pickler(file)
    pickler.dump(data)
    data = file.getvalue()

    if compress_large_data:
        data = zlib.compress(data)

    i = 0
    while len(data) > CACHE_CHUNK_SIZE:
        chunk = data[0:CACHE_CHUNK_SIZE]
        data = data[CACHE_CHUNK_SIZE:]
        cache.set('%s-%d' % (key, i), [chunk], expiration)
        i += 1
    cache.set('%s-%d' % (key, i), [data], expiration)

    cache.set(key, '%d' % (i + 1), expiration)


def cache_memoize(key, lookup_callable,
                  expiration=getattr(settings, "CACHE_EXPIRATION_TIME",
                                     DEFAULT_EXPIRATION_TIME),
                  force_overwrite=False,
                  large_data=False,
                  compress_large_data=True):
    """Memoize the results of a callable inside the configured cache.

    Keyword arguments:
    expiration          -- The expiration time for the key.
    force_overwrite     -- If True, the value will always be computed and stored
                           regardless of whether it exists in the cache already.
    large_data          -- If True, the resulting data will be pickled, gzipped,
                           and (potentially) split up into megabyte-sized chunks.
                           This is useful for very large, computationally
                           intensive hunks of data which we don't want to store
                           in a database due to the way things are accessed.
    compress_large_data -- Compresses the data with zlib compression when
                           large_data is True.
    """
    key = make_cache_key(key)

    if large_data:
        if not force_overwrite and cache.has_key(key):
            try:
                data = _cache_fetch_large_data(cache, key, compress_large_data)
                return data
            except Exception, e:
                logging.warning('Failed to fetch large data from cache for key %s: %s.' % (key, e))
        else:
            logging.debug('Cache miss for key %s.' % key)

        data = lookup_callable()
        _cache_store_large_data(cache, key, data, expiration,
                                compress_large_data)
        return data

    else:
        if not force_overwrite and cache.has_key(key):
            return cache.get(key)
        data = lookup_callable()

        # Most people will be using memcached, and memcached has a limit of 1MB.
        # Data this big should be broken up somehow, so let's warn about this.
        # Users should hopefully be using large_data=True in this case.
        # XXX - since 'data' may be a sequence that's not a string/unicode,
        #       this can fail. len(data) might be something like '6' but the
        #       data could exceed a megabyte. The best way to catch this would
        #       be an exception, but while python-memcached defines an exception
        #       type for this, it never uses it, choosing instead to fail
        #       silently. WTF.
        if len(data) >= CACHE_CHUNK_SIZE:
            logging.warning("Cache data for key %s (length %s) may be too big "
                            "for the cache." % (key, len(data)))

        try:
            cache.set(key, data, expiration)
        except:
            pass
        return data


def make_cache_key(key):
    """Creates a cache key guaranteed to avoid conflicts and size limits.

    The cache key will be prefixed by the site's domain, and will be
    changed to an MD5SUM if it's larger than the maximum key size.
    """
    try:
        site = Site.objects.get_current()

        # The install has a Site app, so prefix the domain to the key.
        # If a SITE_ROOT is defined, also include that, to allow for multiple
        # instances on the same host.
        site_root = getattr(settings, 'SITE_ROOT', None)

        if site_root:
            key = "%s:%s:%s" % (site.domain, site_root, key)
        else:
            key = "%s:%s" % (site.domain, key)
    except:
        # The install doesn't have a Site app, so use the key as-is.
        pass

    # Adhere to memcached key size limit
    if len(key) > MAX_KEY_SIZE:
        digest = new_md5(key).hexdigest();

        # Replace the excess part of the key with a digest of the key
        key = key[:MAX_KEY_SIZE - len(digest)] + digest

    # Make sure this is a non-unicode string, in order to prevent errors
    # with some backends.
    key = str(key)

    return key


def get_object_or_none(klass, *args, **kwargs):
    if isinstance(klass, Manager):
        manager = klass
        klass = manager.model
    else:
        manager = klass._default_manager

    try:
        return manager.get(*args, **kwargs)
    except klass.DoesNotExist:
        return None


def never_cache_patterns(prefix, *args):
    """
    Prevents any included URLs from being cached by the browser.

    It's sometimes desirable not to allow browser caching for a set of URLs.
    This can be used just like patterns().
    """
    pattern_list = []
    for t in args:
        if isinstance(t, (list, tuple)):
            t = url(prefix=prefix, *t)
        elif isinstance(t, RegexURLPattern):
            t.add_prefix(prefix)

        t._callback = never_cache(t.callback)
        pattern_list.append(t)

    return pattern_list



def generate_media_serial():
    """
    Generates a media serial number that can be appended to a media filename
    in order to make a URL that can be cached forever without fear of change.
    The next time the file is updated and the server is restarted, a new
    path will be accessed and cached.

    This will crawl the media files (using directories in MEDIA_SERIAL_DIRS if
    specified, or all of STATIC_ROOT otherwise), figuring out the latest
    timestamp, and return that value.
    """
    MEDIA_SERIAL = getattr(settings, "MEDIA_SERIAL", 0)

    if not MEDIA_SERIAL:
        media_dirs = getattr(settings, "MEDIA_SERIAL_DIRS", ["."])

        for media_dir in media_dirs:
            media_path = os.path.join(settings.STATIC_ROOT, media_dir)

            for root, dirs, files in os.walk(media_path):
                for name in files:
                    mtime = int(os.stat(os.path.join(root, name)).st_mtime)

                    if mtime > MEDIA_SERIAL:
                        MEDIA_SERIAL = mtime

        setattr(settings, "MEDIA_SERIAL", MEDIA_SERIAL)


def generate_ajax_serial():
    """
    Generates a serial number that can be appended to filenames involving
    dynamic loads of URLs in order to make a URL that can be cached forever
    without fear of change.

    This will crawl the template files (using directories in TEMPLATE_DIRS),
    figuring out the latest timestamp, and return that value.
    """
    AJAX_SERIAL = getattr(settings, "AJAX_SERIAL", 0)

    if not AJAX_SERIAL:
        template_dirs = getattr(settings, "TEMPLATE_DIRS", ["."])

        for template_path in template_dirs:
            for root, dirs, files in os.walk(template_path):
                for name in files:
                    mtime = int(os.stat(os.path.join(root, name)).st_mtime)

                    if mtime > AJAX_SERIAL:
                        AJAX_SERIAL = mtime

        setattr(settings, "AJAX_SERIAL", AJAX_SERIAL)


def generate_locale_serial(packages):
    """Generate a locale serial for the given set of packages.

    This will be equal to the most recent mtime of all the .mo files that
    contribute to the localization of the given packages.
    """
    serial = 0

    paths = []
    for package in packages:
        try:
            p = importlib.import_module(package)
            path = os.path.join(os.path.dirname(p.__file__), 'locale')
            paths.append(path)
        except Exception, e:
            logging.error(
                'Failed to import package %s to compute locale serial: %s'
                % (package, e))

    for locale_path in paths:
        for root, dirs, files in os.walk(locale_path):
            for name in files:
                if name.endswith('.mo'):
                    mtime = int(os.stat(os.path.join(root, name)).st_mtime)
                    if mtime > serial:
                        serial = mtime

    return serial


def generate_cache_serials():
    """
    Wrapper around generate_media_serial and generate_ajax_serial to
    generate all serial numbers in one go.

    This should be called early in the startup, such as in the site's
    main urls.py.
    """
    generate_media_serial()
    generate_ajax_serial()
