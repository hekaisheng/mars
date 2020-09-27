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

from ... import opcodes as OperandDef
from ...config import options
from ...core import OutputType
from .core import DataFrameReductionOperand, DataFrameReductionMixin


class DataFrameAll(DataFrameReductionOperand, DataFrameReductionMixin):
    _op_type_ = OperandDef.ALL
    _func_name = 'all'


def all_series(df, axis=None, bool_only=None, skipna=None, level=None, combine_size=None):
    use_inf_as_na = options.dataframe.mode.use_inf_as_na
    op = DataFrameAll(axis=axis, skipna=skipna, level=level, bool_only=bool_only,
                      combine_size=combine_size, output_types=[OutputType.scalar],
                      use_inf_as_na=use_inf_as_na)
    return op(df)


def all_dataframe(df, axis=None, bool_only=None, skipna=None, level=None, combine_size=None):
    use_inf_as_na = options.dataframe.mode.use_inf_as_na
    op = DataFrameAll(axis=axis, skipna=skipna, level=level, bool_only=bool_only,
                      combine_size=combine_size, output_types=[OutputType.series],
                      use_inf_as_na=use_inf_as_na)
    return op(df)
