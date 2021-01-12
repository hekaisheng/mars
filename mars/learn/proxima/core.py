# Copyright 1999-2020 Alibaba Group Holding Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import tempfile

import numpy as np

try:
    import pyproxima2 as proxima
except ImportError:  # pragma: no cover
    proxima = None

from ... import opcodes
from ... import tensor as mt
from ...operands import OutputType
from ...serialize import BoolField, TupleField, DataTypeField, Int64Field, \
    SliceField, StringField
from ...tensor.indexing import TensorSlice
from ..operands import LearnOperand, LearnOperandMixin


available_numpy_dtypes = [
    np.dtype(np.float16),
    np.dtype(np.float32),
    np.dtype(np.int8),
    np.dtype(np.int16),
]


if proxima:
    _proxima_types = [
        proxima.IndexMeta.FT_FP16,
        proxima.IndexMeta.FT_FP32,
        proxima.IndexMeta.FT_INT8,
        proxima.IndexMeta.FT_INT16,
    ]
    assert len(_proxima_types) == len(available_numpy_dtypes)
    _type_mapping = {numpy_dtype: proxima_type
                     for numpy_dtype, proxima_type
                     in zip(available_numpy_dtypes, _proxima_types)}


class ProximaArrayMmap(LearnOperand, LearnOperandMixin):
    _op_type_ = opcodes.PROXIMA_ARRAY_MMAP

    _prefix = StringField('prefix')
    _create_mmap_file = BoolField('create_mmap_file')
    _array_shape = TupleField('array_shape')
    _array_dtype = DataTypeField('array_dtype')
    _offset = Int64Field('offset')
    _partition_slice = SliceField('partition_slice')
    _append_header = BoolField('append_header')

    def __init__(self, create_mmap_file=None, array_shape=None, array_dtype=None,
                 prefix=None, offset=None, partition_slice=None, append_header=None,
                 output_types=None, **kw):
        super().__init__(_create_mmap_file=create_mmap_file,
                         _array_shape=array_shape,
                         _array_dtype=array_dtype,
                         _prefix=prefix, _offset=offset,
                         _append_header=append_header,
                         _partition_slice=partition_slice,
                         _output_types=output_types, **kw)
        if self._output_types is None:
            self._output_types = [OutputType.object]

    @property
    def create_mmap_file(self):
        return self._create_mmap_file

    @property
    def array_shape(self):
        return self._array_shape

    @property
    def array_dtype(self):
        return self._array_dtype

    @property
    def prefix(self):
        return self._prefix

    @property
    def offset(self):
        return self._offset

    @property
    def partition_slice(self):
        return self._partition_slice

    @property
    def append_header(self):
        return self._append_header

    @classmethod
    def execute(cls, ctx, op):
        if op.create_mmap_file:
            path = tempfile.mkstemp(prefix=op.prefix, suffix='.dat')[1]
            if op.append_header:
                fp = np.memmap(path, dtype=op.array_dtype, mode='w+', shape=op.array_shape, offset=128)
                header = np.lib.format.header_data_from_array_1_0(fp)
                with open(path, 'r+b') as f:
                    np.lib.format.write_array_header_1_0(f, header)
            else:
                np.memmap(path, dtype=op.array_dtype, mode='w+', shape=op.array_shape)
            ctx[op.outputs[0].key] = path
        else:
            path = ctx[op.inputs[0].key]
            array = ctx[op.inputs[1].key]
            if op.append_header:
                fp = np.memmap(path, dtype=op.array_dtype, mode='r+', shape=op.array_shape, offset=128)
            else:
                fp = np.memmap(path, dtype=op.array_dtype, mode='r+', shape=op.array_shape)
            fp[op.partition_slice] = array
            ctx[op.outputs[0].key] = path


def rechunk_tensor(tensor, chunk_size):
    chunk_groups = []
    cur_row_number = 0
    cur_chunks = []
    for c in tensor.chunks:
        chunk_nrow = c.shape[0]
        if cur_row_number + chunk_nrow <= chunk_size:
            cur_chunks.append(c)
            cur_row_number += chunk_nrow
        else:
            pos = chunk_size - cur_row_number
            slice_op = TensorSlice((slice(None, pos), slice(None)), dtype=c.dtype)
            cur_chunks.append(slice_op.new_chunk([c], shape=(pos, c.shape[1]),
                                                 index=(len(cur_chunks), 0),
                                                 order=c.order))
            chunk_groups.append(cur_chunks)
            cur_chunks = []
            cur_row_number = 0

            slice_op_rest = TensorSlice((slice(pos, None), slice(None)), dtype=c.dtype)
            rest_chunk = slice_op_rest.new_chunk([c], shape=(chunk_nrow - pos, c.shape[1]),
                                                 index=(len(cur_chunks), 0),
                                                 order=c.order)
            cur_chunks.append(rest_chunk)
            cur_row_number += chunk_nrow - pos

    if len(cur_chunks) > 0:
        chunk_groups.append(cur_chunks)
    return chunk_groups


def build_mmap_chunks(chunks, worker, file_prefix, offset=0,
                      append_header=True):
    write_mmap_chunks = []
    nrows = sum(c.shape[0] for c in chunks)
    array_shape = (nrows, chunks[0].shape[1])
    array_dtype = chunks[0].dtype
    create_mmap_op = ProximaArrayMmap(create_mmap_file=True,
                                      array_shape=array_shape,
                                      array_dtype=array_dtype,
                                      prefix=file_prefix,
                                      append_header=append_header,
                                      offset=offset)
    create_mmap_op._expect_worker = worker
    create_mmap_chunk = create_mmap_op.new_chunk(
        None, index=(0,), shape=(), dtype=array_dtype)
    start_index = 0
    for j, chk in enumerate(chunks):
        s = slice(start_index, start_index + chk.shape[0])
        start_index += chk.shape[0]
        write_mmap_op = ProximaArrayMmap(create_mmap_file=False,
                                         array_shape=array_shape,
                                         array_dtype=array_dtype,
                                         offset=offset,
                                         append_header=append_header,
                                         partition_slice=s)
        write_mmap_op._expect_worker = worker
        write_mmap_chunk = write_mmap_op.new_chunk([create_mmap_chunk, chk],
                                                   index=(j + 1, 0), shape=(),
                                                   dtype=array_dtype)
        write_mmap_chunks.append(write_mmap_chunk)
    return write_mmap_chunks


def validate_tensor(tensor):
    if hasattr(tensor, 'to_tensor'):
        tensor = tensor.to_tensor()
    else:
        tensor = mt.tensor(tensor)
    if tensor.ndim != 2:
        raise ValueError('Input tensor should be 2-d')
    return tensor


def get_proxima_type(np_dtype):
    try:
        return _type_mapping[np_dtype]
    except KeyError:
        raise TypeError(f"Does not support {np_dtype}, available types include "
                        f"{', '.join(t.name for t in _type_mapping)}")
