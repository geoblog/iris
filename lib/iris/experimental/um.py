# (C) British Crown Copyright 2014, Met Office
#
# This file is part of Iris.
#
# Iris is free software: you can redistribute it and/or modify it under
# the terms of the GNU Lesser General Public License as published by the
# Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# Iris is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with Iris.  If not, see <http://www.gnu.org/licenses/>.
"""
Low level support for UM FieldsFile variants.

"""

from __future__ import (absolute_import, division, print_function)

import os
import shutil
import sys

import numpy as np

# Borrow some definitions...
from iris.fileformats.ff import _FF_HEADER_POINTERS, FF_HEADER as _FF_HEADER
from iris.fileformats.pp import _header_defn
from iris.util import create_temp_filename


DEFAULT_WORD_SIZE = 8  # In bytes.


def _make_getter(attr_name, index):
    if isinstance(index, slice):
        def getter(self):
            return tuple(getattr(self, attr_name)[index])
    else:
        def getter(self):
            return getattr(self, attr_name)[index]
    return getter


def _make_setter(attr_name, index):
    def setter(self, value):
        getattr(self, attr_name)[index] = value
    return setter


class _HeaderMetaclass(type):
    """
    Adds convenience get/set properties to the target class
    corresponding to single-valued entries in _FF_HEADER.
        e.g. FixedLengthHeader.sub_model

    Also adds "<name>_start" and "<name>_shape" convenience properties
    corresponding to the "pointer" entries in _FF_HEADER, with the
    exception of the lookup and data components.
        e.g. FixedLengthHeader.integer_constants_start
             FixedLengthHeader.integer_constants_shape

    """
    def __new__(metacls, classname, bases, class_dict):
        def add_property(name, index):
            class_dict[name] = property(_make_getter('_integers', index),
                                        _make_setter('_integers', index))

        for name, offsets in _FF_HEADER:
            if len(offsets) == 1:
                add_property(name, offsets[0])
            elif name in _FF_HEADER_POINTERS:
                if name == 'lookup_table':
                    # Rename for consistency with UM documentation paper F3
                    name = 'lookup'
                elif name == 'data':
                    # Bug fix - data is only 1-dimensional.
                    offsets = offsets[:-1]

                add_property(name + '_start', offsets[0])
                first_offset = offsets[1]
                last_offset = offsets[-1] + 1
                add_property(name + '_shape', slice(first_offset, last_offset))
            else:
                # The remaining multi-value items are 'first_validity_time',
                # 'last_validity_time', and 'misc_validity_time'.
                # But, from the wider perspective of FieldsFile variants
                # these names do not make sense - so we skip them.
                pass

        # Complement to 1-dimensional data bug fix
        add_property('max_length', 161)

        return super(_HeaderMetaclass, metacls).__new__(metacls, classname,
                                                        bases, class_dict)


class FixedLengthHeader(object):
    """
    Represents the FIXED_LENGTH_HEADER component of a UM FieldsFile
    variant.

    Access to simple header items is provided via named attributes,
    e.g. fixed_length_header.sub_model. Other header items can be
    accessed via the :attr:`raw` attribute which provides a simple array
    view of the header.

    """

    __metaclass__ = _HeaderMetaclass

    NUM_WORDS = 256
    IMDI = -32768

    @classmethod
    def empty(cls, word_size=DEFAULT_WORD_SIZE):
        integers = np.empty(cls.NUM_WORDS, dtype='>i{}'.format(word_size))
        integers[:] = cls.IMDI
        return cls(integers)

    @classmethod
    def from_file(cls, source, word_size=DEFAULT_WORD_SIZE):
        """
        Create a FixedLengthHeader from a file-like object.

        Args:

        * source:
            The file-like object to read from.

        Kwargs:

        * word_size:
            The number of bytes in each word of the header.

        """
        integers = np.fromfile(source, dtype='>i{}'.format(word_size),
                               count=cls.NUM_WORDS)
        return cls(integers)

    def __init__(self, integers):
        """
        Create a FixedLengthHeader from the given sequence of integer
        values.

        """
        if len(integers) != self.NUM_WORDS:
            raise ValueError('Incorrect number of words - given {} but should '
                             'be {}.'.format(len(integers), self.NUM_WORDS))
        self._integers = np.asarray(integers)

    def __eq__(self, other):
        try:
            eq = np.all(self._integers == other._integers)
        except AttributeError:
            eq = NotImplemented
        return eq

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is not NotImplemented:
            result = not result
        return result

    @property
    def raw(self):
        return self._integers.view()


# The number of integer header items.
_NUM_FIELD_INTS = 45


class _FieldMetaclass(type):
    """
    Adds human-readable get/set properties derived from a _HEADER_DEFN
    attribute on the target class.
        e.g. field.lbproc, field.blev

    "Array-style" header items, such as LBUSER, result in multiple
    single-valued properties with a one-based numeric suffix.
        e.g. field.lbuser1, field.lbuser7

    """
    def __new__(metacls, classname, bases, class_dict):
        defn = class_dict.get('_HEADER_DEFN')
        if defn is not None:
            for name, indices in defn:
                if len(indices) == 1:
                    names = [name]
                else:
                    names = [name + str(i + 1) for i, _ in enumerate(indices)]
                for name, index in zip(names, indices):
                    if index < _NUM_FIELD_INTS:
                        attr_name = '_int_headers'
                    else:
                        attr_name = '_real_headers'
                        index -= _NUM_FIELD_INTS
                    class_dict[name] = property(_make_getter(attr_name, index),
                                                _make_setter(attr_name, index))
        return super(_FieldMetaclass, metacls).__new__(metacls, classname,
                                                       bases, class_dict)


class Field(object):
    """
    Represents a single entry in the LOOKUP component and its
    corresponding section of the DATA component.

    """
    __metaclass__ = _FieldMetaclass

    #: Zero-based index for lblrec.
    LBLREC_OFFSET = 14
    #: Zero-based index for lbrel.
    LBREL_OFFSET = 21
    #: Zero-based index for lbegin.
    LBEGIN_OFFSET = 28
    #: Zero-based index for lbnrec.
    LBNREC_OFFSET = 29

    def __init__(self, int_headers, real_headers, data_provider):
        """
        Create a Field from the integer headers, the floating-point
        headers, and an object which provides access to the
        corresponding data.

        Args:

        * int_headers:
            A sequence of integer header values.
        * real_headers:
            A sequence of floating-point header values.
        * data_provider:
            Either, an object with a `read_data()` method which will
            provide the corresponding values from the DATA component,
            or a NumPy array.

        """
        self._int_headers = np.asarray(int_headers)
        self._real_headers = np.asarray(real_headers)
        self._data_provider = data_provider

    def __eq__(self, other):
        try:
            eq = (np.all(self._int_headers == other._int_headers) and
                  np.all(self._real_headers == other._real_headers) and
                  np.all(self.read_data() == other.read_data()))
        except AttributeError:
            eq = NotImplemented
        return eq

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is not NotImplemented:
            result = not result
        return result

    def num_values(self):
        """
        Return the number of values defined by this header.

        """
        return len(self._int_headers) + len(self._real_headers)

    def read_data(self):
        """
        Return a NumPy array containing the data for this field.

        Data packed with the WGDOS archive method will be unpacked and
        returned as int/float data as appropriate.

        """
        data = None
        if isinstance(self._data_provider, np.ndarray):
            data = self._data_provider
        elif self._data_provider is not None:
            data = self._data_provider.read_data(self)
        return data


class Field2(Field):
    """
    Represents an entry from the LOOKUP component with a header release
    number of 2.

    """
    _HEADER_DEFN = _header_defn(2)


class Field3(Field):
    """
    Represents an entry from the LOOKUP component with a header release
    number of 3.

    """
    _HEADER_DEFN = _header_defn(3)


# Maps lbrel to a Field class.
_FIELD_CLASSES = {2: Field2, 3: Field3, -99: Field}


# Maps word size and then lbuser1 (i.e. the field's data type) to a dtype.
_DATA_DTYPES = {4: {1: '>f4', 2: '>i4', 3: '>i4'},
                8: {1: '>f8', 2: '>i8', 3: '>i8'}}


_CRAY32_SIZE = 4
_WGDOS_SIZE = 4


class _NormalDataProvider(object):
    """
    Provides access to a simple 2-dimensional array of data, corresponding
    to the data payload for a standard FieldsFile LOOKUP entry.

    """
    def __init__(self, source, offset, word_size):
        self.source = source
        self.offset = offset
        self.word_size = word_size

    def read_data(self, field):
        self.source.seek(self.offset)
        lbpack = field.lbpack
        # Ensure lbpack.n4 (number format) is: native, CRAY, or IEEE.
        format = (lbpack // 1000) % 10
        if format not in (0, 2, 3):
            raise ValueError('Unsupported number format: {}'.format(format))
        lbpack = lbpack % 1000
        # NB. This comparison includes checking for the absence of any
        # compression.
        if lbpack == 0 or lbpack == 2:
            if lbpack == 0:
                word_size = self.word_size
            else:
                word_size = _CRAY32_SIZE
            dtype = _DATA_DTYPES[word_size][field.lbuser1]
            rows = field.lbrow
            cols = field.lbnpt
            data = np.fromfile(self.source, dtype, count=rows * cols)
            data = data.reshape(rows, cols)
        elif lbpack == 1:
            from iris.fileformats.pp_packing import wgdos_unpack
            data_size = ((field.lbnrec * 2) - 1) * _WGDOS_SIZE
            data_bytes = self.source.read(data_size)
            data = wgdos_unpack(data_bytes, field.lbrow, field.lbnpt,
                                field.bmdi)
        else:
            raise ValueError('Unsupported lbpack: {}'.format(field.lbpack))
        return data


class _BoundaryDataProvider(object):
    """
    Provides access to the data payload corresponding to a LOOKUP entry
    in a lateral boundary condition FieldsFile variant.

    The data will be 2-dimensional, with the first dimension expressing
    the number of vertical levels and the second dimension being an
    "unrolled" version of all the boundary points.

    """
    def __init__(self, source, offset, word_size):
        self.source = source
        self.offset = offset
        self.word_size = word_size

    def read_data(self, field):
        self.source.seek(self.offset)
        lbpack = field.lbpack
        # Ensure lbpack.n4 (number format) is: native, CRAY, or IEEE.
        format = (lbpack // 1000) % 10
        if format not in (0, 2, 3):
            raise ValueError('Unsupported number format: {}'.format(format))
        lbpack = lbpack % 1000
        if lbpack == 0 or lbpack == 2:
            if lbpack == 0:
                word_size = self.word_size
            else:
                word_size = _CRAY32_SIZE
            dtype = _DATA_DTYPES[word_size][field.lbuser1]
            data = np.fromfile(self.source, dtype, count=field.lblrec)
            data = data.reshape(field.lbhem - 100, -1)
        else:
            msg = 'Unsupported lbpack for LBC: {}'.format(field.lbpack)
            raise ValueError(msg)
        return data


class FieldsFileVariant(object):
    """
    Represents a single a file containing UM FieldsFile variant data.

    """

    _COMPONENTS = (('integer_constants', 'i'),
                   ('real_constants', 'f'),
                   ('level_dependent_constants', 'f'),
                   ('row_dependent_constants', 'f'),
                   ('column_dependent_constants', 'f'),
                   ('fields_of_constants', 'f'),
                   ('extra_constants', 'f'),
                   ('temp_historyfile', 'i'),
                   ('compressed_field_index1', 'i'),
                   ('compressed_field_index2', 'i'),
                   ('compressed_field_index3', 'i'))

    _WORDS_PER_SECTOR = 2048

    class _Mode(object):
        def __init__(self, name):
            self.name = name

        def __repr__(self):
            return self.name + '_MODE'

    #: The file will be opened for read-only access.
    READ_MODE = _Mode('READ')
    #: The file will be opened for update.
    UPDATE_MODE = _Mode('UPDATE')
    #: The file will be created, overwriting the file if it already
    #: exists.
    CREATE_MODE = _Mode('CREATE')

    _MODE_MAPPING = {READ_MODE: 'rb', UPDATE_MODE: 'r+b', CREATE_MODE: 'wb'}

    def __init__(self, filename, mode=READ_MODE, word_size=DEFAULT_WORD_SIZE):
        """
        Opens the given filename as a UM FieldsFile variant.

        Args:

        * filename:
            The name of the file containing the UM FieldsFile variant.

        Kwargs:

        * mode:
            The file access mode: `READ_MODE` for read-only;
            `UPDATE_MODE` for amending; `CREATE_MODE` for creating a new
            file.

        * word_size:
            The number of byte in each word.

        """
        if mode not in self._MODE_MAPPING:
            raise ValueError('Invaild access mode: {}'.format(mode))

        self._filename = filename
        self._mode = mode
        self._word_size = word_size

        source_mode = self._MODE_MAPPING[mode]
        self._source = source = open(filename, source_mode)

        if mode is self.CREATE_MODE:
            header = FixedLengthHeader.empty(word_size)
        else:
            header = FixedLengthHeader.from_file(source, word_size)
        self.fixed_length_header = header

        def constants(name, dtype):
            start = getattr(self.fixed_length_header, name + '_start')
            if start > 0:
                source.seek((start - 1) * word_size)
                shape = getattr(self.fixed_length_header, name + '_shape')
                values = np.fromfile(source, dtype, count=np.product(shape))
                if len(shape) > 1:
                    values = values.reshape(shape, order='F')
            else:
                values = None
            return values

        for name, kind in self._COMPONENTS:
            dtype = '>{}{}'.format(kind, word_size)
            setattr(self, name, constants(name, dtype))

        int_dtype = '>i{}'.format(word_size)
        real_dtype = '>f{}'.format(word_size)

        if self.fixed_length_header.dataset_type == 5:
            data_class = _BoundaryDataProvider
        else:
            data_class = _NormalDataProvider

        lookup = constants('lookup', int_dtype)
        fields = []
        if lookup is not None:
            if lookup[Field.LBNREC_OFFSET, 0] == 0:
                # A model dump has no direct addressing - only relative,
                # so we need to update the offset as we create each
                # Field.
                running_offset = ((self.fixed_length_header.data_start - 1) *
                                  word_size)
                for raw_headers in lookup.T:
                    if raw_headers[0] == -99:
                        data_provider = None
                    else:
                        offset = running_offset
                        data_provider = data_class(source, offset, word_size)
                    klass = _FIELD_CLASSES[raw_headers[Field.LBREL_OFFSET]]
                    ints = raw_headers[:_NUM_FIELD_INTS]
                    reals = raw_headers[_NUM_FIELD_INTS:].view(real_dtype)
                    fields.append(klass(ints, reals, data_provider))
                    running_offset += (raw_headers[Field.LBLREC_OFFSET] *
                                       word_size)
            else:
                for raw_headers in lookup.T:
                    if raw_headers[0] == -99:
                        data_provider = None
                    else:
                        offset = raw_headers[Field.LBEGIN_OFFSET] * word_size
                        data_provider = data_class(source, offset, word_size)
                    klass = _FIELD_CLASSES[raw_headers[Field.LBREL_OFFSET]]
                    ints = raw_headers[:_NUM_FIELD_INTS]
                    reals = raw_headers[_NUM_FIELD_INTS:].view(real_dtype)
                    fields.append(klass(ints, reals, data_provider))
        self.fields = fields

    def __del__(self):
        if hasattr(self, '_source'):
            self.close()

    def __repr__(self):
        fmt = '<FieldsFileVariant: dataset_type={}>'
        return fmt.format(self.fixed_length_header.dataset_type)

    @property
    def filename(self):
        return self._filename

    @property
    def mode(self):
        return self._mode

    def _prepare_fixed_length_header(self):
        # Set the start locations and dimension lengths(*) in the fixed
        # length header.
        # *) Except for the DATA component where we only determine
        #    the start location.
        header = self.fixed_length_header
        word_number = header.NUM_WORDS + 1  # Numbered from 1.

        # Start by dealing with the normal components.
        for name, kind in self._COMPONENTS:
            value = getattr(self, name)
            start_name = name + '_start'
            shape_name = name + '_shape'
            if value is None:
                setattr(header, start_name, header.IMDI)
                setattr(header, shape_name, header.IMDI)
            else:
                setattr(header, start_name, word_number)
                setattr(header, shape_name, value.shape)
                word_number += value.size

        # Now deal with the LOOKUP and DATA components.
        if self.fields:
            header.lookup_start = word_number
            lengths = set(field.num_values() for field in self.fields)
            if len(lengths) != 1:
                msg = 'Inconsistent header lengths - {}'.format(lengths)
                raise ValueError(msg)
            header.lookup_shape = (lengths.pop(), len(self.fields))

            # Round to the nearest whole number of "sectors".
            offset = word_number - 1
            offset -= offset % -self._WORDS_PER_SECTOR
            header.data_start = offset + 1
        else:
            header.lookup_start = header.IMDI
            header.lookup_shape = header.IMDI
            header.data_start = header.IMDI
            header.data_shape = header.IMDI

    def _write_new(self, filename):
        self._prepare_fixed_length_header()

        # Helper function to ensure an array is big-endian and of the
        # correct dtype kind and word size.
        def normalise(values, kind):
            return values.astype('>{}{}'.format(kind, self._word_size))

        # Create a brand new output file.
        with open(filename, 'wb') as f:
            # Skip the fixed length header. We'll write it at the end
            # once we know how big the DATA component needs to be.
            header = self.fixed_length_header
            f.seek(header.NUM_WORDS * self._word_size)

            # Write all the normal components which have a value.
            for name, kind in self._COMPONENTS:
                values = getattr(self, name)
                if values is not None:
                    f.write(buffer(normalise(values, kind)))

            if self.fields:
                # Skip the LOOKUP component and write the DATA component.
                # We need to adjust the LOOKUP headers to match where
                # the DATA payloads end up, so to avoid repeatedly
                # seeking backwards and forwards it makes sense to wait
                # until we've adjusted them all and write them out in
                # one go.
                f.seek((header.data_start - 1) * self._word_size)
                dataset_type = self.fixed_length_header.dataset_type
                pad_fields = dataset_type not in (1, 2)
                sector_size = self._WORDS_PER_SECTOR * self._word_size
                for field in self.fields:
                    data = field.read_data()
                    if data is not None:
                        field.lbegin = f.tell() / self._word_size
                        size = data.size
                        size -= size % -self._WORDS_PER_SECTOR
                        field.lbnrec = size
                        kind = {1: 'f', 2: 'i', 3: 'i'}.get(field.lbuser1,
                                                            data.dtype.kind)
                        f.write(normalise(data, kind))
                        if pad_fields:
                            overrun = f.tell() % sector_size
                            if overrun != 0:
                                f.write(np.zeros(sector_size - overrun, 'i1'))

                # Update the fixed length header to reflect the extent
                # of the DATA component.
                if dataset_type == 5:
                    header.data_shape = 0
                elif self.fields:
                    header.data_shape = ((f.tell() // self._word_size) -
                                         header.data_start + 1)

                # Go back and write the LOOKUP component.
                f.seek((header.lookup_start - 1) * self._word_size)
                for field in self.fields:
                    f.write(buffer(normalise(field._int_headers, 'i')))
                    f.write(buffer(normalise(field._real_headers, 'f')))

            # Write the fixed length header - now that we know how big
            # the DATA component was.
            f.seek(0)
            f.write(normalise(self.fixed_length_header.raw, 'i'))

    def close(self):
        """
        When appropriate write out any pending changes, and close the
        underlying file.

        If the file was opened for update or creation then any changes
        to the fixed length header, the constant components (e.g.
        integer_constants, level_dependent_constants), or the list of
        fields are written to the file before closing.

        Irrespective of the initial file mode, any subsequent
        modifications to any attributes will have no effect on the
        underlying file. Calling `close()` more than once is allowed.

        """
        if not self._source.closed:
            try:
                if self.mode in (self.UPDATE_MODE, self.CREATE_MODE):
                    # For simplicity at this stage we always create a new
                    # file and rename it once complete.
                    # At some later stage we can optimise for in-place
                    # modifications, for example if only one of the integer
                    # constants has been modified.
                    tmp_filename = create_temp_filename()
                    self._write_new(tmp_filename)
                    os.unlink(self.filename)
                    shutil.move(tmp_filename, self.filename)
            finally:
                self._source.close()
