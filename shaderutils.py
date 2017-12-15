#!/usr/bin/env python3
#-*- coding:utf-8 -*-

from __future__ import print_function
from __future__ import division
from __future__ import unicode_literals

import struct
import collections

try:
    import tkinter as t
    from tkinter import filedialog
    from tkinter import messagebox
except ImportError:
    import Tkinter as t 
    import tkFileDialog as filedialog
    import tkMessageBox as messagebox

FILECACHE_MAGIC = 0x8371b694
FILECACHE_MAGIC_V2 = 0x8371b695
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

class ShaderCache:
    def __init__(self, data):
        self.original_size = len(data)
        self.header = unpack_header(data)
        print(self.header)

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
        self.filename = None
        self.merged = False
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
            text='Open shader', command=self._on_btnopen)
        self.btnopen.pack(side=t.LEFT)
        self.btnsave = t.Button(frmbuttons, 
            text='Save shader', command=self._on_btnsave, state=t.DISABLED)
        self.btnsave.pack(side=t.LEFT)
        self.btnmerge = t.Button(frmbuttons, 
            text='Merge shader', command=self._on_btnmerge, state=t.DISABLED)
        self.btnmerge.pack(side=t.LEFT)



    def read_shadercache(self, filename):
        with open(filename, 'rb') as f:
            data = f.read()
        return ShaderCache(data)

    def _on_btnopen(self):
        filename = filedialog.askopenfilename(
            title='Select shader cache file', 
            initialdir=None,
            filetypes=[('binary files', '*.bin')],
            parent=self.main,
        )
        if not filename:
            return
        shadercache = self.read_shadercache(filename)
        self.filename = filename
        self.shadercache = shadercache
        self.merged = False
        self.update_display()

    def _on_btnsave(self):
        with open(self.filename, 'wb') as f:
            self.shadercache.write(f)
        self.merged = False
#        self.shadercache = self.read_shadercache(self.filename)
        self.update_display()
        

    def _on_btnmerge(self):
        filename = filedialog.askopenfilename(
            title='Select another shader cache file to merge', 
            initialdir=None,
            filetypes=[('binary files', '*.bin')],
            parent=self.main,
        )
        if not filename:
            return
        if filename == self.filename:
            messagebox.showerror(
                title='Invalid file', 
                message="You can't merge the file with itself", 
                parent=self.main
            )
        shadermerge = self.read_shadercache(filename)

        main_keys = set(self.shadercache.entries.keys())
        merge_keys = set(shadermerge.entries.keys())
        
        new_keys = merge_keys - main_keys
        duplicate_keys = merge_keys & main_keys
        
        if len(new_keys) == 0:
            messagebox.showinfo(
                title='File empty', 
                message="You already have all shaders in this file, nothing to do", 
                parent=self.main
            )
            return
        
        display = (
            'This cache file has {total_merge} shaders.\n'
            '{dups} are duplicates\n'
            '{news} will be added.\n'
            'Are you sure you want to merge?'
        ).format(
            total_merge=len(shadermerge.entries) - 1,
            dups=len(duplicate_keys) - 1,
            news=len(new_keys),
        )
        if messagebox.askyesno(title='Confirm merge', message=display, 
                default=messagebox.NO, parent=self.main):
            for key in new_keys:
                self.shadercache.entries[key] = shadermerge.entries[key]
            self.shadercache.update_header()
            self.merged = True
            self.update_display()

    def update_display(self):
        display = []
        if self.filename:
            display.append(self.filename)
            display.append('Loaded {num_shaders} shaders.'.format(
                num_shaders=len(self.shadercache.entries) - 1, # remove the table itself from the count
                num_bytes=self.shadercache.original_size,
            ))
            if self.merged:
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
        else:
            display.append('Choose a shader cache file to begin')
        self.displayvar.set('\n'.join(display))
        

if __name__ == '__main__':
    root = t.Tk()
    root.resizable(0, 0)
    root.title('Shader Pack Tools for CEMU')
    app = ShaderUtils(root)
    root.mainloop()


