"""
Copyright (c) 2012 Timon Wong

Permission is hereby granted, free of charge, to any person obtaining a copy of
this software and associated documentation files (the "Software"), to deal in
the Software without restriction, including without limitation the rights to
use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies
of the Software, and to permit persons to whom the Software is furnished to do
so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import sublime

import base64
import imp
import inspect
import mimetypes
import os
import re
import sys
import threading
from urlparse import urlparse
from time import time

from OmniMarkupLib.Setting import Setting
from OmniMarkupLib.Common import entities_unescape, Singleton, RWLock
from OmniMarkupLib import LibraryPathManager
from OmniMarkupLib import log

# HACK: Make sure required base_renderer package load first
import OmniMarkupLib.Renderers.base_renderer
OmniMarkupLib.Renderers.base_renderer  # Prevent PEP8 Warning

__file__ = os.path.normpath(os.path.abspath(__file__))
__path__ = os.path.dirname(__file__)


LibraryPathManager.push_search_path(os.path.dirname(sys.executable))
LibraryPathManager.push_search_path(os.path.join(__path__, 'libs'))
try:
    from bottle import template
finally:
    LibraryPathManager.pop_search_path()
    LibraryPathManager.pop_search_path()


# Test filesystem case sensitivity
# http://stackoverflow.com/questions/7870041/check-if-file-system-is-case-insensitive-in-python
g_fs_case_sensitive = True


def check_filesystem_case_sensitivity():
    import tempfile
    global g_fs_case_sensitive
    fd, path = tempfile.mkstemp()
    if os.path.exists(path.upper()):
        g_fs_case_sensitive = False
    else:
        g_fs_case_sensitive = True
    os.close(fd)
    os.remove(path)
check_filesystem_case_sensitivity()


# @Singleton
# class OpenedBufferManager(object):
#     def __init__(self):
#         self.rwlock = RWLock()
#         self.buffers = {}

#     def add_or_update(self, view):
#         if not g_fs_case_sensitive:
#             fn = view.file_name().lower()
#         pass

#     def remove(self, view):
#         del self.buffers[buffer_id]
#         is_closed = True
#         pass


class RenderedMarkupCacheEntry(dict):
    __getattr__ = dict.__getitem__
    __setattr__ = dict.__setitem__
    __delattr__ = dict.__delitem__

    def __init__(self, timestamp=None, filename='', dirname='', html_part=''):
        timestamp = timestamp or str(time())
        for name, val in locals().iteritems():
            if name == 'self':
                continue
            self[name] = val
        self['__deepcopy__'] = self.__deepcopy__

    def __deepcopy__(self, memo={}):
        return self.copy()


@Singleton
class RenderedMarkupCache(object):
    def __init__(self):
        self.rwlock = RWLock()
        self.cache = {}

    def exists(self, buffer_id):
        with self.rwlock.readlock:
            return buffer_id in self.cache

    def get_entry(self, buffer_id):
        with self.rwlock.readlock:
            if buffer_id in self.cache:
                return self.cache[buffer_id]
        return None

    def set_entry(self, buffer_id, entry):
        with self.rwlock.writelock:
            self.cache[buffer_id] = entry

    def clean(self, keep_ids=set()):
        with self.rwlock.writelock:
            remove_ids = set(self.cache.keys())
            remove_ids -= keep_ids
            if len(remove_ids) == 0:
                return
            for buffer_id in remove_ids:
                del self.cache[buffer_id]
            log.info("Clean buffer ids in: %s" % list(remove_ids))


class WorkerQueueItem(object):
    def __init__(self, buffer_id, timestamp=0, fullpath='untitled', lang='', text=''):
        self.buffer_id = buffer_id
        self.timestamp = timestamp
        self.fullpath = fullpath or 'untitled'
        self.lang = lang
        self.text = text

    def __cmp__(self, other):
        return self.buffer_id == other.buffer_id

    def __hash__(self):
        return hash(self.buffer_id)


class RendererWorker(threading.Thread):
    def __init__(self, mutex):
        threading.Thread.__init__(self)
        self.cond = threading.Condition(mutex)
        self.que = set()
        self.stopping = False

    def enqueue(self, buffer_id, fullpath, lang, text, immediate=False):
        item = WorkerQueueItem(buffer_id, fullpath=fullpath, lang=lang, text=text)
        if immediate:  # Render in the main thread
            self._run_queued_item(item)
        else:
            with self.cond:
                self.que.add(item)
                self.cond.notify()

    def _run_queued_item(self, item):
        try:
            # Render text and save to cache
            filename = os.path.basename(item.fullpath)
            dirname = os.path.dirname(item.fullpath)
            html_part = RendererManager.render_text(item.fullpath, item.lang, item.text)
            entry = RenderedMarkupCacheEntry(filename=filename, dirname=dirname, html_part=html_part)
            RenderedMarkupCache.instance().set_entry(item.buffer_id, entry)
        except NotImplementedError:
            pass
        except:
            log.exception("")

    def run(self):
        while True:
            with self.cond:
                self.cond.wait()
                if self.stopping:
                    break
                if len(self.que) == 0:
                    continue
            for item in list(self.que):
                self._run_queued_item(item)
            self.que.clear()

    def stop(self):
        self.stopping = True
        with self.cond:
            self.cond.notify()
        self.join()


class RendererManager(object):
    MUTEX = threading.Lock()
    WORKER = RendererWorker(MUTEX)

    LANG_RE = re.compile(r"^[^\s]+(?=\s+)")
    RENDERERS = []

    @classmethod
    def has_any_valid_renderer(cls, filename, lang):
        # filename may be None, so prevent it
        filename = filename or ""
        for renderer_classname, renderer in cls.RENDERERS:
            if renderer.is_enabled(filename, lang):
                return True
        return False

    @classmethod
    def get_lang_by_scope_name(cls, scope_name):
        m = cls.LANG_RE.search(scope_name)
        if m is None:
            lang = ""
        else:
            lang = m.group(0).lower()
        return lang

    @classmethod
    def has_renderer_enabled_in_view(cls, view):
        filename = view.file_name()
        lang = cls.get_lang_by_scope_name(view.scope_name(0))
        return cls.has_any_valid_renderer(filename, lang)

    @classmethod
    def render_text(cls, fullpath, lang, text, post_process_func=None):
        if post_process_func is None:
            post_process_func = cls.render_text_postprocess
        filename = os.path.basename(fullpath)
        for renderer_classname, renderer in cls.RENDERERS:
            try:
                if renderer.is_enabled(filename, lang):
                    rendered_text = renderer.render(text, filename=filename)
                    return post_process_func(rendered_text, fullpath)
            except:
                log.exception('Exception occured while rendering using %s', renderer_classname)
        raise NotImplementedError()

    IMG_TAG_RE = re.compile('(<img [^>]*src=")([^"]+)("[^>]*>)', re.DOTALL | re.IGNORECASE | re.MULTILINE)

    @classmethod
    def render_text_postprocess(cls, rendered_text, filename):
        dirname = os.path.dirname(filename)

        def encode_image_path(m):
            url = m.group(2)
            o = urlparse(url)
            if len(o.scheme) > 0:
                # Is a valid url, returns original text
                return m.group(0)
            # or local file (maybe?)
            local_path = os.path.normpath(os.path.join(dirname, entities_unescape(url)))
            return m.group(1) + '/local/' + base64.urlsafe_b64encode(local_path.encode('utf-8')) + m.group(3)

        return cls.IMG_TAG_RE.sub(encode_image_path, rendered_text)

    @classmethod
    def render_text_postprocess_exporting(cls, rendered_text, filename):
        # Embedding images
        dirname = os.path.dirname(filename)

        def encode_image_path(m):
            url = m.group(2)
            o = urlparse(url)
            if len(o.scheme) > 0:
                # Is a valid url, returns original text
                return m.group(0)
            # or local file (maybe?)
            local_path = os.path.normpath(os.path.join(dirname, entities_unescape(url)))
            mime_type, _ = mimetypes.guess_type(os.path.basename(local_path))
            if mime_type is not None:
                data_uri = open(local_path, 'rb').read().encode('base64').replace('\n', '')
                image_tag_src = 'data:%s;base64,%s' % (mime_type, data_uri)
            else:
                image_tag_src = '[Invalid mime type]'
            return m.group(1) + image_tag_src + m.group(3)

        return cls.IMG_TAG_RE.sub(encode_image_path, rendered_text)

    @classmethod
    def render_view_to_string(cls, view):
        fullpath = view.file_name() or ''
        lang = RendererManager.get_lang_by_scope_name(view.scope_name(0))
        text = view.substr(sublime.Region(0, view.size()))
        html_part = RendererManager.render_text(
            fullpath, lang, text,
            post_process_func=cls.render_text_postprocess_exporting
        )
        setting = Setting.instance()
        return template(setting.export_options['template_name'],
                        mathjax_enabled=setting.mathjax_enabled,
                        filename=os.path.basename(fullpath),
                        dirname=os.path.dirname(fullpath),
                        html_part=html_part)

    @classmethod
    def enqueue_view(cls, view, only_exists=False, immediate=False):
        buffer_id = view.buffer_id()
        settings = view.settings()
        if only_exists and not RenderedMarkupCache.instance().exists(buffer_id):
            # If current view is previously rendered, then ignore 'only_exists'
            if not settings.get('omnimarkup_enabled', False):
                return
        settings.set('omnimarkup_enabled', True)
        region = sublime.Region(0, view.size())
        text = view.substr(region)
        lang = cls.get_lang_by_scope_name(view.scope_name(0))
        cls.WORKER.enqueue(buffer_id, view.file_name(), lang, text, immediate=immediate)

    @classmethod
    def _import_module(cls, name, path, prefix=None):
        if prefix and isinstance(prefix, str):
            modname = "%s.%s" % (prefix, name)
        else:
            modname = name

        f, filename, etc = imp.find_module(name, [path])
        mod = imp.load_module(modname, f, filename, etc)
        return mod

    @classmethod
    def _load_renderer(cls, renderers, path, module_name):
        prefix = 'OmniMarkupLib.Renderers'
        try:
            mod = cls._import_module(module_name, path, prefix)
            # Get classes
            classes = inspect.getmembers(mod, inspect.isclass)
            for classname, classtype in classes:
                # Register renderer into manager
                if hasattr(classtype, 'IS_VALID_RENDERER__'):
                    try:
                        log.info('Loaded renderer: %s', classname)
                        # Add both classname and its instance
                        renderers.append((classname, classtype()))
                    except:
                        log.exception('Failed to load renderer: %s', classname)
        except:
            log.exception('Failed to load renderer module: %s.%s', prefix, module_name)

    @classmethod
    def load_renderers(cls):
        renderers = []
        with cls.MUTEX:
            # Add library path to sys.path
            LibraryPathManager.push_search_path(os.path.dirname(sys.executable))
            LibraryPathManager.add_search_path_if_not_exists(os.path.join(__path__, './Renderers/libs/'))

            # Change the current directory to that of the module. It's not safe to just
            # add the modules directory to sys.path, as that won't accept unicode paths
            # on Windows
            renderers_path = os.path.join(__path__, 'Renderers/')
            oldpath = os.getcwdu()
            os.chdir(os.path.join(__path__, '..'))

            try:
                module_list = [f for f in os.listdir(renderers_path)
                               if f.endswith('Renderer.py')]
                # Load each renderer
                for module_file in module_list:
                    cls._load_renderer(renderers, renderers_path, module_file[:-3])
            finally:
                # Restore the current directory
                os.chdir(oldpath)
                LibraryPathManager.pop_search_path()
        cls.RENDERERS = renderers

    OLD_IGNORED_RENDERERS = set()

    @classmethod
    def on_setting_changing(cls, setting):
        cls.OLD_IGNORED_RENDERERS = setting.ignored_renderers.copy()

    @classmethod
    def on_setting_changed(cls, setting):
        # Unload ignored renderers
        if cls.OLD_IGNORED_RENDERERS != setting.ignored_renderers:
            # Reload renderers, of course
            log.info('Reloading renderers...')
            cls.load_renderers()

        for renderer_classname, renderer in cls.RENDERERS:
            key = 'renderer_options-' + renderer_classname
            try:
                renderer_options = setting._sublime_settings.get(key, {})
                renderer.load_settings(renderer_options, setting)
            except:
                log.exception('Error on setting renderer options for %s', renderer_classname)

    WAIT_TIMEOUT = 1.0
    STARTED = True
    RENDERERS_LOADING_THREAD = None

    @classmethod
    def ensure_started(cls):
        if cls.RENDERERS_LOADING_THREAD is not None:
            try:
                cls.RENDERERS_LOADING_THREAD.join(cls.WAIT_TIMEOUT)
            except:
                pass
        return cls.STARTED

    @classmethod
    def start(cls):
        cls.STARTED = True

        setting = Setting.instance()
        setting.subscribe('changing', cls.on_setting_changing)
        setting.subscribe('changed', cls.on_setting_changed)

        cls.WORKER.start()
        cls.on_setting_changing(setting)

        # HACK: OmniMarkupLib.Renderers must be recognizable to thread
        #import OmniMarkupLib.Renderers

        def f():
            log.info("Loading renderers...")
            cls.load_renderers()
            sublime.set_timeout(lambda: cls.on_setting_changed(setting), 0)
            cls.STARTED = True
            cls.RENDERERS_LOADING_THREAD = None
        cls.RENDERERS_LOADING_THREAD = threading.Thread(target=f)
        sublime.set_timeout(lambda: cls.RENDERERS_LOADING_THREAD.start(), 0)

    @classmethod
    def stop(cls):
        cls.WORKER.stop()
        if cls.RENDERERS_LOADING_THREAD is not None:
            try:
                cls.RENDERERS_LOADING_THREAD.join()
            except:
                pass
        cls.STARTED = False
