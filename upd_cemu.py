from __future__ import print_function
from __future__ import unicode_literals

import re
import os
import sys
import json
import errno
import shutil
import aiohttp
import asyncio
import zipfile
import argparse
import subprocess
import collections
import urllib.request
import logging.handlers
logger = logging.getLogger('upd_cemu')

import tkinter as t
import tkinter.ttk as ttk

__version__ = 'beta2'

if getattr(sys, 'frozen', False):
    BASEDIR = os.path.dirname(sys.executable)
else:
    BASEDIR = os.path.abspath(os.path.dirname(__file__))

CONFIG_FILENAME = os.path.join(BASEDIR, 'upd_cemu.json')

GITHUB_API_URL = 'https://api.github.com/repos/slashiee/cemu_graphic_packs/releases/latest'
IDS_TO_DETECT = set([
    '00050001', # demo
    '00050000', # game
])


def hide_file(filename):
    try:
        import win32file
        import win32con
        import win32api
    except ImportError:
        pass
    else:
        flags = win32file.GetFileAttributesW(filename)
        win32file.SetFileAttributes(filename, win32con.FILE_ATTRIBUTE_HIDDEN | flags)


def remove_path(path):
    try:
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        else:
            shutil.rmtree(path)
    except OSError as e:
        if e.errno != errno.ENOENT:
            raise

def create_path(path, remove_first=False):
    if remove_first:
        remove_path(path)
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
    return path

def get_latest_pack_name_url():
    logger.debug('Requesting last zip information from github...')
    with urllib.request.urlopen(GITHUB_API_URL) as f:
        data = json.loads(f.read().decode())
    asset = data['assets'][0]
    return asset['name'], asset['browser_download_url']

def download_pack(url, filename):
    if os.path.exists(filename):
        logger.info('file already exists, using existing file')
        return False
    else:
        logger.info('Downloading %r', url)
        urlretrieve(
            url,
            filename + '.incomplete',
        )
        shutil.move(filename + '.incomplete', filename)
        return True

def detect_games(cemu_mlc_dir):
    games = set()
    try:
        save_path = os.path.join(cemu_mlc_dir, 'usr', 'save')
        logger.debug('Searching for games in %r', cemu_mlc_dir)
        for hi_id in os.listdir(save_path):
            if len(hi_id) != 8 or hi_id not in IDS_TO_DETECT:
                continue
            id_subfolder = os.path.join(save_path, hi_id)
            if not os.path.isdir(id_subfolder):
                continue
            for lo_id in os.listdir(id_subfolder):
                if len(lo_id) != 8:
                    continue
                if os.path.isdir(os.path.join(id_subfolder, lo_id)):
                    games.add((hi_id + lo_id).upper())
    except OSError:
        pass
    return games


class DownloadCancelled(Exception): pass
class DownloadError(Exception): pass

class DownloadProgress:
    def __init__(self, root, title='Downloading...'):
        self.root = root
        self.cancelled = False
        root.title(title)
        root.protocol('WM_DELETE_WINDOW', self.on_close)
        self.main = t.Frame(root)
        self.create_widgets(self.main)
        self.main.pack()

    def create_widgets(self, parent):
        self.var_label = t.StringVar()
        self.var_label.set('Initializing...')
        t.Label(parent, textvariable=self.var_label).grid(row=0, sticky=t.W)

        self.pb = ttk.Progressbar(
            parent, 
            length=600, 
            mode='indeterminate', 
            orient=t.HORIZONTAL,
            maximum=100,
        )
        self.pb.grid(row=1, sticky=t.W + t.E)
        self.pb.start()

    def on_close(self):
        self.cancelled = True
        self.var_label.set('Cancelling...')
        self.cancel_download()

    def update_progress_bar(self, count, total_size):
        if total_size == -1 or self.cancelled:
            self.pb.config(mode='indeterminate')
            self.pb.start()
        else:
            self.pb.stop()
            self.pb.config(mode='determinate', 
                maximum=total_size, value=count)

async def _download(url, filename, update_ui_callback=None):
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            size = int(resp.headers.get('content-length', -1))
            with open(filename, 'wb') as fd:
                count = 0
                while True:
                    chunk = await resp.content.readany()
                    if not chunk:
                        break
                    count += len(chunk)
                    fd.write(chunk)
                    update_ui_callback(count, size)
    return filename

async def update_tkinter_ui(root, timer=0.1):
    while True:
        root.update()
        await asyncio.sleep(timer)

def urlretrieve(url, filename, title='Downloading...'):
    root = t.Tk()
    root.resizable(0, 0)
    app = DownloadProgress(root, title)
    app.var_label.set(url)

    ui_task = asyncio.ensure_future(update_tkinter_ui(root))
    download_task = asyncio.ensure_future(_download(
        url, filename, app.update_progress_bar))

    app.cancel_download = download_task.cancel

    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(download_task)
        return download_task.result()
    finally:
        ui_task.cancel()
        root.destroy()


def generate_config(**config):
    _cfg = {
        'cemu_path': BASEDIR,
        'download_dir': BASEDIR,
        'delete_downloads': True,
        'resolution_file': os.path.join(BASEDIR, 'res.txt'),
        'resolutions': set(),
        'last_graphic_packs': None,
        'last_cemuhook': None,
        'keep_dir': os.path.join(BASEDIR, 'vault'),
        'gameid_list': set(),
        'exec_cemu': True,
        'extra_params': [],
        'fullscreen': True,
        'update_gameid_list': True,
    }
    _cfg.update(config)
    return _cfg

def read_config(filename=CONFIG_FILENAME):
    logger.info('Reading config from %r', filename)
    try:
        with open(filename) as f:
            config = json.load(f)
    except OSError:
        logger.warning('Error reading config file', exc_info=True)
        config = None
    except json.JSONDecodeError:
        logger.warning('Error decoding config file', exc_info=True)
        config = None
    else:
        config['resolutions'] = set(tuple(res)
            for res in config.get('resolutions', ()))
        config['gameid_list'] = set(config.get('gameid_list', ()))
    return config

def write_config(config, filename=CONFIG_FILENAME):
    logger.debug('Writing config to %r', filename)
    create_path(os.path.dirname(filename))
    config_write = config.copy()
    for key_remove in ('downloaded', 'command_line_args'):
        config_write.pop(key_remove, None)
    for key_list in ('gameid_list', 'resolutions'):
        config_write[key_list] = list(config.get(key_list, ()))
    with open(filename, 'w') as f:
        json.dump(config_write, f, indent=4)

def extract_packs(zipfile, config):
    packs_by_gameid, pack_files = read_packs(zipfile)
    packs_to_unpack = set()
    _ids_to_unpack = config['gameid_list']
    if not _ids_to_unpack:
        _ids_to_unpack = packs_by_gameid.keys() # unpack all
    for _id in _ids_to_unpack:
        if _id not in packs_by_gameid:
            continue
        _packs_res = packs_by_gameid[_id]
        if not config['resolutions']:
            logger.debug('Extracting all packs for game %s', _id)
            for _packs in _packs_res.values():
                packs_to_unpack.update(_packs) # all resolutions
        else:
            logger.debug('Filtering resolutions for game %s', _id)
            packs_to_unpack.update(_packs_res.get(None, ()))
            for res in config['resolutions']:
                if res in _packs_res:
                    packs_to_unpack.update(_packs_res[res])

    files_to_unpack = []
    for packname in packs_to_unpack:
        files_to_unpack.extend(pack_files[packname])
    destination_path = os.path.join(config['cemu_path'], 'graphicPacks')
    backup_path = os.path.join(config['cemu_path'], 'graphicPacks_old')
    control_file = os.path.join(destination_path, '.upd_cemu')
    if (os.path.exists(destination_path) and
            (not os.path.exists(control_file)) and
            (not os.path.exists(backup_path))):
        logger.debug('Creating backup %r', backup_path)
        shutil.move(destination_path, backup_path)
    logger.debug('Creating destination path')
    create_path(destination_path, remove_first=True)
    with open(control_file, 'w'): pass
    hide_file(control_file)
    if not files_to_unpack:
        logger.debug('Unpacking all files')
        files_to_unpack = None # unpack all
    zipfile.extractall(destination_path, members=files_to_unpack)

def link_keep_dir(config):
    src_path = config['keep_dir']
    dst_path = os.path.join(config['cemu_path'], 'graphicPacks')
    if not os.path.isdir(src_path):
        return False
    for packname in os.listdir(src_path):
        full_packdir = os.path.join(src_path, packname)
        if not os.path.isdir(full_packdir):
            continue
        try:
            with open(os.path.join(full_packdir, 'rules.txt'), 'rb') as f:
                pack_game_ids, res = _parse_rules_txt(f)
        except OSError:
            logger.warning("Vault pack %r doesn't have rules.txt, skipping",
                pack_name)
            continue
        else:
            _ids_to_unpack = config['gameid_list']
            if not _ids_to_unpack or (pack_game_ids & _ids_to_unpack):
                logger.debug('Copying vault pack %r', packname)
                dst_packdir = os.path.join(dst_path, packname)
                remove_path(dst_packdir)
                shutil.copytree(full_packdir, dst_packdir)
            else:
                logger.debug('Ignoring vault pack %r', packname)


def _parse_rules_txt(f,
            _re_res = re.compile(r'"?.* - (\d+)x(\d+)(?: \(\d+:\d+\))?\s*"?$', re.I),
            _re_titleids = re.compile('titleids\s*=', re.I),
            _re_name=re.compile(r'name\s*=', re.I),
        ):
    pack_game_ids = None
    pack_name = None
    res = None
    for line in f:
        line = line.decode().strip()
        if _re_titleids.match(line):
            pack_game_ids = set(_id.strip().upper()
                for _id in line.split('=', 1)[1].strip().split(','))
        if _re_name.match(line):
            pack_name = line.split('=', 1)[1].strip()
        if pack_game_ids is not None and pack_name is not None:
            break
    if pack_name:
        m = _re_res.match(pack_name)
        if m:
            res = tuple(int(x) for x in m.groups())
    return pack_game_ids, res

def read_packs(zipfile):
    all_packs = collections.defaultdict(list)
    packs_by_gameid = collections.defaultdict(lambda: collections.defaultdict(set))
    for info in zipfile.infolist():
        packname = info.filename.split('/', 1)[0]
        all_packs[packname].append(info)
        if info.filename.endswith('/rules.txt') and info.filename.count('/') == 1:
            with zipfile.open(info) as f:
                pack_game_ids, res = _parse_rules_txt(f)
            if pack_game_ids is not None:
                for _id in pack_game_ids:
                    packs_by_gameid[_id][res].add(packname)
    logger.debug('%d packs found in zip', len(all_packs))
    return packs_by_gameid, all_packs

_re_resspec = re.compile(r'(\d+)[x*,. -](\d+)$', re.I)
_re_resspec2 = re.compile(r'(\d+)p?\s*(uw|ultra\s*wide)?$', re.I)
_WIDE = 1.7777777777777777
_UWIDE = 2.3703703703703702
_UWIDE_SPECIAL = {
    1440: (3440, 1440),
}
_NICKNAMES = [
    ((800, 600), ('svga',)),
    ((1024, 600), ('wsvga',)),
    ((1280, 720), ('hd',)),
    ((1600, 900), ('hd+',)),
    ((1920, 1080), ('fhd', 'fullhd', 'full-hd', 'wide', 'widescreen')),
    ((2560, 1080), ('ultrawide', 'ultra-wide', 'uw', 'uwfhd', 'uw-fhd')),
    ((2560, 1440), ('2k', 'qhd')),
    ((3440, 1440), ('2kuw', '2kultrawide', 'uwqhd')),
    ((3200, 1800), ('3k', 'qhd+')),
    ((3840, 2160), ('4k', 'uhd', '4kuhd')),
    ((5120, 2160), ('5kuw', '5kultrawide', 'uw5k', 'uhd+')),
    ((7680, 4320), ('8k', '8kuhd')),
]
_NICKNAMES = {
    nick: res for res, nicks in _NICKNAMES for nick in nicks
}

def detect_res(text):
    text = text.strip().lower()
    m = _re_resspec.match(text)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _re_resspec2.match(text)
    if m:
        height = int(m.group(1))
        if m.group(2):
            if height in _UWIDE_SPECIAL:
                return _UWIDE_SPECIAL[height]
            else:
                return int(height * _UWIDE), height
        else:
            return int(height * _WIDE), height
    return _NICKNAMES.get(text.replace(' ', ''))


def read_resolution_file(filename):
    resolutions = set()
    try:
        with open(filename) as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning('Error reading resolutions from %r', filename)
        return resolutions
    for line in lines:
        res = detect_res(line)
        if not res:
            logger.debug('Ignoring line in resolution file: %r', line)
            continue
        resolutions.add(res)
    return resolutions

def update_gameid_list(config):
    mlc_dir = getattr(config.get('command_line_args'), 'mlc', None)
    if not mlc_dir:
        mlc_dir = os.path.join(config.get('cemu_path', BASEDIR), 'mlc01')
    new_games = detect_games(mlc_dir)
    if new_games != config['gameid_list']:
        logger.debug('Games changed: %r', new_games)
        config['gameid_list'] = new_games
        return True
    else:
        logger.info('No change in games.')
    return False

def detect_changes_resolutions(config):
    resolutions = read_resolution_file(config['resolution_file'])
    if config['resolutions'] != resolutions:
        logger.info('Resolutions file changed: %r', resolutions)
        config['resolutions'] = resolutions
        return True
    else:
        logger.info('No change in resolutions.')
        return False

def detect_changes_gameid_list(config):
    if config.get('update_gameid_list'):
        return update_gameid_list(config)
    else:
        logger.debug('Skipping game id search')
        return False

def detect_changes_zip_pack(config, need_file=False):
    zip_name, zip_url = get_latest_pack_name_url()
    last_filename = config['last_graphic_packs']
    if last_filename is None or zip_name != last_filename:
        logger.info('New zip url detected %r', zip_url)
        config['last_graphic_packs'] = zip_name
        modified = True
    else:
        logger.info('No change in repository download name')
        modified = False
    if modified or need_file:
        zip_fullname = os.path.join(config['download_dir'], zip_name)
        config['downloaded'] = download_pack(zip_url, zip_fullname)
    return modified

def detect_changes(config, ):
    modified = detect_changes_resolutions(config)
    modified = detect_changes_gameid_list(config) or modified
    modified = detect_changes_zip_pack(config, need_file=modified) or modified
    return modified

def exec_cemu(cemu_path, extra_args=None):
    logger.info('Trying to execute cemu')
    if os.path.exists(os.path.join(cemu_path, 'Cemu.exe')):
        command_line = ['Cemu.exe']
        if extra_args:
             command_line.extend(extra_args)
        os.chdir(cemu_path)
        logger.debug('Command line: %r', command_line)
        if sys.platform == 'linux':
            logger.debug('Linux detected, using wine to start cemu')
            command_line.insert(0, 'wine')
            os.execvp(command_line[0], command_line)
        else: # windows
            logging.debug('Platform: %s', sys.platform)
            try:
                subprocess.Popen(command_line, close_fds=True)
#                os.execv(command_line[0], command_line)
            except OSError as e:
                if e.winerror != 740:
                    raise # unknown error running cemu
                import ctypes
                ctypes.windll.shell32.ShellExecuteW(
                    None, #hwnd
                    "runas", #lpOperation
                    command_line[0], #lpFile
                    subprocess.list2cmdline(command_line[1:]), #lpParameters
                    None, #lpDirectory
                    1  #nShowCmd
                )
    else:
        logger.warning('Cemu.exe not found.')


def unpack_packs(config):
    create_path(config['download_dir'])
    zip_fullname = os.path.join(config['download_dir'],
                                config['last_graphic_packs'])
    with zipfile.ZipFile(zip_fullname) as zf:
        extract_packs(zf, config)
    if config['delete_downloads'] and config.get('downloaded'):
        logging.info('Removing downloaded file %r', zip_fullname)
        remove_path(zip_fullname)
        remove_path(zip_fullname + '.incomplete')

_formatter = logging.Formatter(
    fmt='{asctime}: {levelname} {module}({lineno}) {message}',
    style='{',
)

def _configure_logging():
    _root_logger = logging.getLogger()
    _stream_log = logging.StreamHandler()
    _stream_log.setFormatter(_formatter)
    _stream_log.setLevel(logging.DEBUG)
    _memory_log = logging.handlers.MemoryHandler(5000)
    _memory_log.setLevel(logging.DEBUG)
    _root_logger.addHandler(_stream_log)
    _root_logger.addHandler(_memory_log)
    _root_logger.setLevel(logging.DEBUG)
    return _memory_log

def parse_args():
    logger.debug('Command line: %r', sys.argv)
    parser = argparse.ArgumentParser(prog='upd_cemu')
    parser.add_argument('game', help="The game you want to run", nargs='?')
    parser.add_argument('-g', '--game', dest='game_compat', 
        help="Another way to specify the game, for compatibility with cemu")
    parser.add_argument('-mlc', help="location of cemu mlc folder")
    parser.add_argument('-c', '--config', default=CONFIG_FILENAME,
        help="Config file to read instead of upd_cemu.json")
    args, extra = parser.parse_known_args()
    if args.game is None:
        args.game = args.game_compat
    return args, extra

if __name__ == '__main__':
    log_filename = os.path.join(BASEDIR, 'upd_cemu_crashlog.txt')
    memory_log = _configure_logging()

    logging.info('Initializing version %s (%s)', __version__, 
        'frozen' if getattr(sys, 'frozen', False) else 'not frozen')
    args, extra = parse_args()

    try:
        config = read_config(args.config)
        if config:
            extra_params = config.get('extra_params', None)
            if extra_params:
                # reparse command line with the extra args from config
                sys.argv.extend(extra_params)
                args, extra = parse_args() 
        else:
            config = generate_config()
            logger.info('New config generated!')
        config['command_line_args'] = args
        logger.debug('Config: %r', config)
        try:
            if detect_changes(config):
                logger.info('Changes detected. Executing unpack')
                unpack_packs(config)
                write_config(config, args.config)
        except DownloadCancelled:
            logger.exception('Download cancelled by user, not updating')

        link_keep_dir(config)
        if config['exec_cemu']:
            if args.game:
                extra.extend(['-g', args.game])
                if config.get('fullscreen', True) and '-f' not in extra:
                    extra.append('-f')
            if args.mlc:
                extra.extend(['-mlc', args.mlc])
            exec_cemu(config['cemu_path'], extra)
    except:
        logger.exception('Fatal Error in main')
        logger.critical('Flushing log to file %r', log_filename)
        _file_log = logging.FileHandler(log_filename, 'w')
        _file_log.setLevel(logging.DEBUG)
        _file_log.setFormatter(_formatter)
        memory_log.setTarget(_file_log)
        memory_log.flush()

