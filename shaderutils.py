#!/usr/bin/env python3
#-*- coding:utf-8 -*-

from __future__ import print_function
from __future__ import division
from __future__ import unicode_literals

import re
import os
import errno
import struct
import shutil
import collections

try:
    import tkinter as t
    from tkinter import filedialog
    from tkinter import messagebox
except ImportError:
    import Tkinter as t 
    import tkFileDialog as filedialog
    import tkMessageBox as messagebox

DEFAULT_PATH = None

FILECACHE_MAGIC = 0x8371b694
FILECACHE_MAGIC_V2 = 0x8371b695
SHADER_HEADER_MAGIC = 0xc8d47908
#FILECACHE_HEADER_RESV = 128 # number of bytes reserved for the header
FILECACHE_FILETABLE_NAME1 = 0xEFEFEFEFEFEFEFEF
FILECACHE_FILETABLE_NAME2 = 0xFEFEFEFEFEFEFEFE
FILECACHE_FILETABLE_FREE_NAME = 0

def _packer_unpacker_factory(format_list):
    fmt, names = zip(*format_list)
    fmt = '=' + ''.join(fmt)
    names = [name for name in names if name is not None]
    s = struct.Struct(fmt)
    def _packer(data_dict, _s=s, _names=names):
        return _s.pack(*(data_dict[name] for name in _names))
    
    def _unpacker(data, offset=0, _s=s, _names=names):
        return dict(zip(_names, _s.unpack_from(data, offset)))
    return _packer, _unpacker, s.size

pack_entry, unpack_entry, ENTRY_SIZE = _packer_unpacker_factory([
        ('Q', 'name1'),         #	uint64 name1;   
        ('Q', 'name2'),         #	uint64 name2;
        ('Q', 'file_offset'),   #	uint64 fileOffset;
        ('I', 'file_size'),     #	uint32 fileSize;
        ('I', 'reserved'),      #	uint32 extraReserved; // currently unused, 
        #but in the future may be used to extend fileSize or add flags (for compression)
])

pack_header, unpack_header, FILECACHE_HEADER_RESV = _packer_unpacker_factory([
    ('I', 'magic'), #	uint32 headerMagic = stream_readU32(stream_file);
    ('I', 'extra_version'),  #	uint32 headerExtraVersion = stream_readU32(stream_file);
    ('Q', 'data_offset'),       #	headerDataOffset = stream_readU64(stream_file);
    ('Q', 'file_table_offset'), #	headerFileTableOffset = stream_readU64(stream_file);
    ('I', 'file_table_size'),   #	uint32 headerFileTableSize = stream_readU32(stream_file);
    ('100x', None),          # filler to 128 bytes
])

pack_shader_header, unpack_shader_header, SHADER_HEADER_SIZE = _packer_unpacker_factory([
    ('I', 'magic'), # 4 byte constant, always 0xC8D47908
    ('I', 'type'),  # 4 byte shader type
    ('Q', 'name1'), # 8 byte shader base name (base hash)
    ('Q', 'name2'), # 8 byte shader sub name (aux hash)
])

shader_type_names = {
    0: 'VERTEX',   #define SHADER_CACHE_TYPE_VERTEX    (0)
    1: 'GEOMETRY', #define SHADER_CACHE_TYPE_GEOMETRY  (1)
    2: 'PIXEL',    #define SHADER_CACHE_TYPE_PIXEL     (2)
}


class ShaderCache:
    def __init__(self, data=None):
        if data is None:
            self.original_size = 0
            self.header = {
                'magic': FILECACHE_MAGIC_V2,
                'extra_version': 1,
                'data_offset': FILECACHE_HEADER_RESV,
            }
            table_entry = {
                'name1': FILECACHE_FILETABLE_NAME1,
                'name2': FILECACHE_FILETABLE_NAME2,
                'reserved': 0,
            }
            self.entries = collections.OrderedDict([
                (
                    (FILECACHE_FILETABLE_NAME1, FILECACHE_FILETABLE_NAME2),
                    table_entry,
                )
            ])
        else:

            self.original_size = len(data)
            self.header = unpack_header(data)

            # verify file consistency
            assert self.header['magic'] == FILECACHE_MAGIC_V2
            assert self.header['file_table_size'] % ENTRY_SIZE == 0
            entry_count = self.header['file_table_size'] // ENTRY_SIZE

            # read the table
            offset = self.header['data_offset'] + self.header['file_table_offset']
            entries = (unpack_entry(data, offset + (ENTRY_SIZE * n)) 
                for n in range(entry_count))
            self.entries = collections.OrderedDict(
                ((e['name1'], e['name2']), e) for e in entries
            )
            
            if (FILECACHE_FILETABLE_FREE_NAME, FILECACHE_FILETABLE_FREE_NAME) in self.entries:
                del self.entries[FILECACHE_FILETABLE_FREE_NAME, FILECACHE_FILETABLE_FREE_NAME]
            
            # split the data
            for entry in self.entries.values():
                begin = self.header['data_offset'] + entry['file_offset']
                end = begin + entry['file_size']
                entry['data'] = data[begin:end]

        # update the header
        self.update_header()

    def update_header(self):
        self.header['file_table_offset'] = 0
        self.header['file_table_size'] = ENTRY_SIZE * len(self.entries)
        table_entry = self.entries[FILECACHE_FILETABLE_NAME1, FILECACHE_FILETABLE_NAME2]
        table_entry['file_offset'] = self.header['file_table_offset']
        table_entry['file_size'] = self.header['file_table_size']
        table_entry['data'] = None

    def calc_size(self):
        return FILECACHE_HEADER_RESV + sum(entry['file_size'] for entry in self.entries.values())

    def write(self, f):
        # write the header
        bin_header = pack_header(self.header)
        assert len(bin_header) == FILECACHE_HEADER_RESV
        f.write(bin_header)

        # store the encoded table itself as a entry in first position of the table
        current_offset = 0
        table_data = b''
        for entry in self.entries.values():
            entry['file_offset'] = current_offset
            table_data += pack_entry(entry)
            current_offset += entry['file_size']
        self.entries[FILECACHE_FILETABLE_NAME1, FILECACHE_FILETABLE_NAME2]['data'] = table_data

        # write all data
        for entry in self.entries.values():
            assert len(entry['data']) == entry['file_size']
            f.write(entry['data'])

        self.original_size = self.calc_size()

class ShaderUtils:
    def __init__(self, parent):
        self.shadercache = None
        self.filename = None
        self.modified = False
        self.main = t.Frame(parent)
        self.make_layout(self.main)
        self.main.pack()
        self.displayvar = t.StringVar()
        t.Label(parent, textvariable=self.displayvar).pack()
        self.update_display()

    def make_layout(self, parent):
        frmbuttons = t.Frame(parent)
        frmbuttons.pack()

        self.btnopen = t.Button(frmbuttons, 
            text='Open cache', command=self._on_btnopen)
        self.btnopen.pack(side=t.LEFT)
        self.btnsave = t.Button(frmbuttons, 
            text='Save cache', command=self._on_btnsave, state=t.DISABLED)
        self.btnsave.pack(side=t.LEFT)
        self.btnmerge = t.Button(frmbuttons, 
            text='Merge another', command=self._on_btnmerge, 
            state=t.DISABLED)
        self.btnmerge.pack(side=t.LEFT)
        self.btnfixshader = t.Button(frmbuttons,
            text='Unpack cache', command=self._on_unpack, 
            state=t.DISABLED)
        self.btnfixshader.pack(side=t.LEFT)


    def read_shadercache(self, filename):
        with open(filename, 'rb') as f:
            data = f.read()
        return ShaderCache(data)

    def _on_btnopen(self):
        filenames = filedialog.askopenfilenames(
            title='Select shader cache files', 
            initialdir=DEFAULT_PATH,
            filetypes=[('binary files', '*.bin')],
            parent=self.main,
        )
        if len(filenames) == 0:
            return

        shadercache = self.read_shadercache(filenames[0])
        print(shadercache.header)
        if len(filenames) == 1:
            self.filename = filenames[0]
            self.modified = False
        else:
            self.filename = None
            self.modified = True

        self.shadercache = shadercache

        for filename in filenames[1:]:
            self.shadercache.entries.update(self.read_shadercache(filename).entries)

        self.shadercache.update_header()
        self.update_display()

    def _on_btnsave(self):
        if self.filename is None:
            filename = filedialog.asksaveasfilename(
                title='File to save as',
                initialdir=DEFAULT_PATH,
                filetypes=[('Shader cache files', '*.bin')],
                parent=self.main,
            )
            if not filename:
                return
            self.filename = filename
        with open(self.filename, 'wb') as f:
            self.shadercache.write(f)
        self.modified = False
        self.update_display()


    def _on_btnmerge(self):
        filenames = filedialog.askopenfilenames(
            title='Select shader cache files to merge', 
            initialdir=DEFAULT_PATH,
            filetypes=[('Shader cache files', '*.bin')],
            parent=self.main,
        )
        if len(filenames) == 0: # cancelled
            return
        if self.filename in filenames:
            messagebox.showerror(
                title='Invalid file', 
                message="You can't merge the file with itself", 
                parent=self.main
            )

        all_entries = collections.OrderedDict()
        for filename in filenames:
            all_entries.update(self.read_shadercache(filename).entries)

        main_keys = set(self.shadercache.entries.keys())
        merge_keys = set(all_entries.keys())
        
        new_keys = merge_keys - main_keys
        duplicate_keys = merge_keys & main_keys
        
        if len(new_keys) == 0:
            messagebox.showinfo(
                title='File empty', 
                message="You already have all shaders in these files, nothing to do", 
                parent=self.main
            )
            return

        display = (
            'The files have {total_merge} unique shaders.\n'
            '{dups} are duplicates\n'
            '{news} will be added.\n'
            'Are you sure you want to merge?'
        ).format(
            total_merge=len(all_entries)-1,
            dups=len(duplicate_keys),
            news=len(new_keys),
        )
        if messagebox.askyesno(title='Confirm merge', message=display, 
                default=messagebox.NO, parent=self.main):
            for key in new_keys:
                self.shadercache.entries[key] = all_entries[key]
            self.shadercache.update_header()
            self.modified = True
            self.update_display()

    def _on_unpack(self):
        folder = os.path.splitext(self.filename)[0]
        try:
            os.makedirs(folder)
        except OSError as e:
            if e.errno != errno.EEXIST:
                raise
            if messagebox.askyesno(title='Unpack error', 
                    message=('Folder {f} already exists.\n'
                        'Delete it first?').format(
                            f=folder,
                        )
                    ):
                shutil.rmtree(folder)
                os.makedirs(folder)
            else:
                return

        for entry in self.shadercache.entries.values():
            if (entry['name1'] == FILECACHE_FILETABLE_NAME1 
                    and entry['name2'] ==  FILECACHE_FILETABLE_NAME2):
                continue
            entry = entry.copy() 

            s = ShaderCache()
            s.entries[entry['name1'], entry['name2']] = entry
            s.update_header()

            shader_header = unpack_shader_header(entry['data'])

            filename = '{type}_{name1:016x}_{name2:016x}.bin'.format(
                name1=shader_header['name1'],
                type=shader_type_names.get(shader_header['type'],
                    'UNKNOWN_{t}'.format(t=shader_header['type'])),
                name2=shader_header['name2'],
            )
            with open(os.path.join(folder, filename), 'wb') as f:
                s.write(f)

    def update_display(self):
        display = []
        if self.shadercache is not None:
            if self.filename:
                display.append(self.filename)
            else:
                display.append('File name not set. Press save to choose file name')
                self.modified = True
            display.append('Loaded {num_shaders} shaders.'.format(
                num_shaders=len(self.shadercache.entries) - 1, # remove the table itself from the count
                num_bytes=self.shadercache.original_size,
            ))
            if self.modified:
                display.append("The merged keys are not saved. Don't forget to press the save button.")
                self.btnsave['state'] = t.NORMAL
            else:
                new_size = self.shadercache.calc_size()
                if self.shadercache.original_size > new_size:
                    display.append('If you save the file it will be optimized reducing size by {num_bytes} bytes'.format(
                        num_bytes=self.shadercache.original_size - new_size,
                    ))
                    self.btnsave['state'] = t.NORMAL
                else:
                    display.append('This file is already optimized.'.format(
                        num_bytes=self.shadercache.original_size - self.shadercache.calc_size(),
                    ))
                    self.btnsave['state'] = t.DISABLED
            self.btnmerge['state'] = t.NORMAL
            self.btnfixshader['state'] = t.NORMAL
        else:
            display.append('Choose a shader cache file to begin')
            self.btnsave['state'] = t.DISABLED
            self.btnmerge['state'] = t.DISABLED
            self.btnfixshader['state'] = t.DISABLED
        self.displayvar.set('\n'.join(display))


if __name__ == '__main__':
    root = t.Tk()
    root.resizable(0, 0)
    root.title('Shader Pack Tools v4 for CEMU')
    app = ShaderUtils(root)
    root.mainloop()


