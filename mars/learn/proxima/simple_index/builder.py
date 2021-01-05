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

import itertools
import logging
import os
import pickle  # nosec  # pylint: disable=import_pickle
import tempfile

import numpy as np

from .... import opcodes
from .... import tensor as mt
from ....context import get_context, RunningMode
from ....filesystem import get_fs, LocalFileSystem
from ....operands import OutputType, OperandStage
from ....serialize import KeyField, StringField, Int32Field, Int64Field, \
    DictField, BytesField, BoolField, ListField, DataTypeField, SliceField
from ....tensor.indexing import TensorSlice
from ....tiles import TilesError
from ....utils import check_chunks_unknown_shape, Timer
from ...operands import LearnOperand, LearnOperandMixin
from ..core import proxima, get_proxima_type, validate_tensor, available_numpy_dtypes

logger = logging.getLogger(__name__)


DEFAULT_INDEX_SIZE = 5 * 10 ** 6


class ProximaBuildMmap(LearnOperand, LearnOperandMixin):
    _op_type_ = opcodes.PROXIMA_BUILD_MMAP

    _create_mmap_file = BoolField('create_mmap_file')
    _array_shape = ListField('array_shape')
    _array_dtype = DataTypeField('array_dtype')
    _offset = Int64Field('offset')
    _partition_slice = SliceField('partition_slice')

    def __init__(self, create_mmap_file=None, array_shape=None, array_dtype=None,
                 offset=None, partition_slice=None, output_types=None, **kw):
        super().__init__(_create_mmap_file=create_mmap_file,
                         _array_shape=array_shape,
                         _array_dtype=array_dtype,
                         _offset=offset,
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
    def offset(self):
        return self._offset

    @property
    def partition_slice(self):
        return self._partition_slice

    @classmethod
    def execute(cls, ctx, op):
        if op.create_mmap_file:
            path = tempfile.mkstemp(prefix='proxima-mmap-', suffix='.dat')[1]
            np.memmap(path, dtype=op.array_dtype, mode='w+', shape=op.array_shape)
            ctx[op.outputs[0].key] = path
        else:
            path = ctx[op.inputs[0].key]
            array = ctx[op.inputs[1].key]
            fp = np.memmap(path, dtype=op.array_dtype, mode='r+', shape=op.array_shape)
            fp[op.partition_slice] = array
            ctx[op.outputs[0].key] = path


class ProximaBuilder(LearnOperand, LearnOperandMixin):
    _op_type_ = opcodes.PROXIMA_SIMPLE_BUILDER

    _distance_metric = StringField('distance_metric')
    _dimension = Int32Field('dimension')
    _column_number = Int64Field('column_number')
    _index_path = StringField('index_path')
    _index_builder = StringField('index_builder')
    _index_builder_params = DictField('index_builder_params')
    _index_converter = StringField('index_converter')
    _index_converter_params = DictField('index_converter_params')
    _topk = Int32Field('topk')
    _storage_options = BytesField('storage_options',
                                  on_serialize=pickle.dumps,
                                  on_deserialize=pickle.loads)

    def __init__(self, distance_metric=None, index_path=None,
                 dimension=None, column_number=None,
                 index_builder=None, index_builder_params=None,
                 index_converter=None, index_converter_params=None,
                 topk=None, storage_options=None, output_types=None, stage=None, **kw):
        super().__init__(_distance_metric=distance_metric, _index_path=index_path,
                         _dimension=dimension, _column_number=column_number, _index_builder=index_builder,
                         _index_builder_params=index_builder_params,
                         _index_converter=index_converter, _index_converter_params=index_converter_params,
                         _topk=topk, _storage_options=storage_options,
                         _output_types=output_types, _stage=stage, **kw)
        if self._output_types is None:
            self._output_types = [OutputType.object]

    @property
    def distance_metric(self):
        return self._distance_metric

    @property
    def column_number(self):
        return self._column_number

    @property
    def index_path(self):
        return self._index_path

    @property
    def dimension(self):
        return self._dimension

    @property
    def index_builder(self):
        return self._index_builder

    @property
    def index_builder_params(self):
        return self._index_builder_params

    @property
    def index_converter(self):
        return self._index_converter

    @property
    def index_converter_params(self):
        return self._index_converter_params

    @property
    def topk(self):
        return self._topk

    @property
    def storage_options(self):
        return self._storage_options

    def __call__(self, tensor):
        return self.new_tileable([tensor])

    @classmethod
    def _get_atleast_topk_nsplit(cls, nsplit, topk):
        new_nsplit = []
        i = 0
        while i < len(nsplit):
            cur = nsplit[i]
            i += 1
            if cur >= topk:
                new_nsplit.append(cur)
            else:
                while i < len(nsplit):
                    cur += nsplit[i]
                    i += 1
                    if cur >= topk:
                        break
                if cur < topk and len(new_nsplit) > 0:
                    new_nsplit[-1] += cur
                elif cur >= topk:
                    new_nsplit.append(cur)
        new_nsplit = tuple(new_nsplit)
        assert sum(new_nsplit) == sum(nsplit), f'sum of nsplit not equal, ' \
                                               f'old: {nsplit}, new: {new_nsplit}'

        return new_nsplit

    @classmethod
    def tile(cls, op):
        tensor = op.inputs[0]
        out = op.outputs[0]
        index_path = op.index_path
        ctx = get_context()
        fs = None
        if index_path is not None:
            fs = get_fs(index_path, op.storage_options)

        # check index_path for distributed
        if getattr(ctx, 'running_mode', None) == RunningMode.distributed:
            if index_path is not None:
                if isinstance(fs, LocalFileSystem):
                    raise ValueError('`index_path` cannot be local file dir '
                                     'for distributed index building')

        if index_path is not None:
            # check if the index path is empty
            try:
                files = [f for f in fs.ls(index_path) if 'proxima_' in f]
                if files:
                    raise ValueError(f'Directory {index_path} contains built proxima index, '
                                     f'clean them to perform new index building')
            except FileNotFoundError:
                # if not exist, create directory
                fs.mkdir(index_path)

        # make sure all inputs have known chunk sizes
        check_chunks_unknown_shape(op.inputs, TilesError)

        if op.column_number:
            index_chunk_size = op.inputs[0].shape[0] // op.column_number
        else:
            worker_num = len(ctx.get_worker_addresses() or [])
            if worker_num > 0:
                index_chunk_size = max(op.inputs[0].shape[0] // worker_num, DEFAULT_INDEX_SIZE)
            else:
                index_chunk_size = DEFAULT_INDEX_SIZE

        if op.topk is not None:
            index_chunk_size = cls._get_atleast_topk_nsplit(index_chunk_size, op.topk)

        # build chunks for writing tensors to mmap files.
        index_chunk_inputs = []
        cur_row_number = 0
        cur_chunks = []
        for c in tensor.chunks:
            chunk_nrow = c.shape[0]
            if cur_row_number + chunk_nrow <= index_chunk_size:
                cur_chunks.append(c)
                cur_row_number += chunk_nrow
            else:
                pos = index_chunk_size - cur_row_number
                slice_op = TensorSlice((slice(None, pos), slice(None)), dtype=c.dtype)
                cur_chunks.append(slice_op.new_chunk([c], shape=(pos, c.shape[1]),
                                                     index=(len(cur_chunks), 0),
                                                     order=c.order))
                index_chunk_inputs.append(cur_chunks)
                cur_chunks = []
                cur_row_number = 0

                slice_op_rest = TensorSlice((slice(pos, None), slice(None)), dtype=c.dtype)
                rest_chunk = slice_op_rest.new_chunk([c], shape=(chunk_nrow - pos, c.shape[1]),
                                                     index=(len(cur_chunks), 0),
                                                     order=c.order)
                cur_chunks.append(rest_chunk)
                cur_row_number += chunk_nrow - pos

        if len(cur_chunks) > 0:
            index_chunk_inputs.append(cur_chunks)

        workers = ctx.get_worker_addresses()
        worker_iter = iter(itertools.cycle(workers))
        out_chunks = []
        offset = 0
        for i, chunks in enumerate(index_chunk_inputs):
            write_mmap_chunks = []
            worker = next(worker_iter)
            nrows = sum(c.shape[0] for c in chunks)
            array_shape = (nrows, chunks[0].shape[1])
            array_dtype = chunks[0].dtype
            create_mmap_op = ProximaBuildMmap(create_mmap_file=True,
                                              array_shape=array_shape,
                                              array_dtype=array_dtype,
                                              offset=offset)
            create_mmap_op._expect_worker = worker
            create_mmap_chunk = create_mmap_op.new_chunk(
                None, index=(0,), shape=(), dtype=array_dtype)
            start_index = 0
            for j, chk in enumerate(chunks):
                s = slice(start_index, start_index + chk.shape[0])
                start_index += chk.shape[0]
                write_mmap_op = ProximaBuildMmap(create_mmap_file=False,
                                                 array_shape=array_shape,
                                                 array_dtype=array_dtype,
                                                 offset=offset,
                                                 partition_slice=s)
                write_mmap_op._expect_worker = worker
                write_mmap_chunk = write_mmap_op.new_chunk([create_mmap_chunk, chk],
                                                           index=(j, 0), shape=(),
                                                           dtype=array_dtype)
                write_mmap_chunks.append(write_mmap_chunk)
            offset += nrows
            out_chunks.append(write_mmap_chunks)

        out_chunks = []
        for j, chunks in enumerate(out_chunks):
            chunk_op = op.copy().reset_key()
            chunk_op._stage = OperandStage.map
            chunk_op._expect_worker = chunks[0].op.expect_worker
            out_chunk = chunk_op.new_chunk(chunks, index=(j,))
            out_chunks.append(out_chunk)

        logger.warning(f"index chunks count: {len(out_chunks)} ")

        params = out.params
        params['chunks'] = out_chunks
        params['nsplits'] = ((1,) * len(out_chunks),)
        new_op = op.copy()
        return new_op.new_tileables(op.inputs, kws=[params])

    @classmethod
    def _execute_map(cls, ctx, op: "ProximaBuilder"):
        inp = op.inputs[0]
        path = ctx[inp.key]
        out = op.outputs[0]

        data = np.memmap(path, dtype=inp.op.array_dtype, mode='r',
                         shape=inp.op.array_shape)

        proxima_type = get_proxima_type(inp.dtype)
        offset = inp.op.offset

        # holder
        with Timer() as timer:
            holder = proxima.IndexHolder(type=proxima_type,
                                         dimension=op.dimension, shallow=True)
            holder.mount(data)

        logger.warning(f'Holder({op.key}) costs {timer.duration} seconds')

        # converter
        meta = proxima.IndexMeta(proxima_type, dimension=op.dimension,
                                 measure_name=op.distance_metric)
        if op.index_converter is not None:
            with Timer() as timer:
                converter = proxima.IndexConverter(name=op.index_converter,
                                                   meta=meta, params=op.index_converter_params)
                converter.train_and_transform(holder)
                holder = converter.result()
                meta = converter.meta()

            logger.warning(f'Converter({op.key}) costs {timer.duration} seconds')

        # builder
        with Timer() as timer:
            builder = proxima.IndexBuilder(name=op.index_builder,
                                           meta=meta,
                                           params=op.index_builder_params)
            builder = builder.train_and_build(holder)

        logger.warning(f'Builder({op.key}) costs {timer.duration} seconds')

        # dumper
        with Timer() as timer:
            path = tempfile.mkstemp(prefix='proxima-', suffix='.index')[1]
            dumper = proxima.IndexDumper(name="FileDumper", path=path)
            builder.dump(dumper)
            dumper.close()

        logger.warning(f'Dumper({op.key}) costs {timer.duration} seconds')

        if op.index_path is None:
            ctx[out.key] = path
        else:
            # write to external file
            with Timer() as timer:
                fs = get_fs(op.index_path, op.storage_options)
                filename = f'proxima_{out.index[0]}_index'
                out_path = f'{op.index_path.rstrip("/")}/{filename}'
                with fs.open(out_path, 'wb') as out_f:
                    with open(path, 'rb') as in_f:
                        # 32M
                        chunk_bytes = 32 * 1024 ** 2
                        while True:
                            data = in_f.read(chunk_bytes)
                            if data:
                                out_f.write(data)
                            else:
                                break

            logger.warning(f'WritingToVolume({op.key}), out path: {out_path}, '
                           f'size {os.path.getsize(path)}, '
                           f'costs {timer.duration} seconds '
                           f'speed {round(os.path.getsize(path) / (1024 ** 2) / timer.duration, 2)} MB/s')

            ctx[out.key] = filename

    @classmethod
    def _execute_agg(cls, ctx, op: "ProximaBuilder"):
        paths = [ctx[inp.key] for inp in op.inputs]
        ctx[op.outputs[0].key] = paths

    @classmethod
    def execute(cls, ctx, op: "ProximaBuilder"):
        if op.stage != OperandStage.agg:
            return cls._execute_map(ctx, op)
        else:
            return cls._execute_agg(ctx, op)

    @classmethod
    def concat_tileable_chunks(cls, tileable):
        assert not tileable.is_coarse()

        op = cls(stage=OperandStage.agg)
        chunk = cls(stage=OperandStage.agg).new_chunk(tileable.chunks)
        return op.new_tileable([tileable], chunks=[chunk], nsplits=((1,),))


def build_index(tensor, dimension=None, index_path=None, column_number=None,
                need_shuffle=False, distance_metric='SquaredEuclidean',
                index_builder='SsgBuilder', index_builder_params=None,
                index_converter=None, index_converter_params=None,
                topk=None, storage_options=None,
                run=True, session=None, run_kwargs=None):
    tensor = validate_tensor(tensor)
    if tensor.dtype not in available_numpy_dtypes:
        raise ValueError(f'Dtype to build index should be one of {available_numpy_dtypes}, '
                         f'got {tensor.dtype}')

    if dimension is None:
        dimension = tensor.shape[1]
    if index_builder_params is None:
        index_builder_params = {}
    if index_converter_params is None:
        index_converter_params = {}

    if need_shuffle:
        tensor = mt.random.permutation(tensor)

    op = ProximaBuilder(tensor=tensor, distance_metric=distance_metric,
                        index_path=index_path, dimension=dimension,
                        column_number=column_number,
                        index_builder=index_builder,
                        index_builder_params=index_builder_params,
                        index_converter=index_converter,
                        index_converter_params=index_converter_params,
                        topk=topk, storage_options=storage_options)
    result = op(tensor)
    if run:
        return result.execute(session=session, **(run_kwargs or dict()))
    else:
        return result
