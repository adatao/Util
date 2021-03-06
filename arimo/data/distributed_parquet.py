import math
import numpy
import os
import psutil
import random
import re
import time
import tqdm
import types
from urllib.parse import urlparse
import uuid

from pyarrow.parquet import ParquetDataset
from s3fs import S3FileSystem

from pyspark.ml import Transformer
from pyspark.ml.feature import SQLTransformer
from pyspark.sql import DataFrame

import arimo.util.data_backend
from .parquet import AbstractS3ParquetDataHandler, \
    _S3ParquetDataFeeder__getitem__pandasDFTransform, _S3ParquetDataFeeder__drop__pandasDFTransform, \
    _S3ParquetDataFeeder__fillna__pandasDFTransform, _S3ParquetDataFeeder__prep__pandasDFTransform, \
    _S3ParquetDataFeeder__pieceArrowTableFunc, _S3ParquetDataFeeder__gen
from arimo.util import fs, Namespace
from arimo.util.aws import s3
from arimo.util.date_time import gen_aux_cols
from arimo.util.decor import enable_inplace
from arimo.util.iterables import to_iterable
from arimo.util.types.spark_sql import _BINARY_TYPE, _STR_TYPE
import arimo.debug

from .distributed import DDF


@enable_inplace
class S3ParquetDistributedDataFrame(AbstractS3ParquetDataHandler, DDF):
    # "inplace-able" methods
    _INPLACE_ABLE = \
        '__call__', \
        '_subset', \
        'drop', \
        'fillna', \
        'filter', \
        'filterByPartitionKeys', \
        'prep', \
        'select', \
        'sql', \
        'transform', \
        'withColumn'

    _CACHE = {}

    # *****************
    # METHODS TO CREATE
    # __init__
    # load

    def __init__(
            self, path, aws_access_key_id=None, aws_secret_access_key=None, reCache=False,
            _initSparkDF=None, _sparkDFTransforms=[], _sparkDF=None,
            _pandasDFTransforms=[],
            reprSampleMinNPieces=AbstractS3ParquetDataHandler._REPR_SAMPLE_MIN_N_PIECES,
            verbose=True, **kwargs):
        if verbose or arimo.debug.ON:
            logger = self.class_stdout_logger()

        self.path = path

        if (not reCache) and (path in self._CACHE):
            _cache = self._CACHE[path]
            
        else:
            self._CACHE[path] = _cache = Namespace()

        if _cache:
            assert _cache._srcSparkDF, \
                '*** FAILED to Load SparkDF from {} ***'.format(path)

            if arimo.debug.ON:
                logger.debug('*** RETRIEVING CACHE FOR {} ***'.format(path))

        else:
            if verbose:
                msg = 'Loading {} by Arrow...'.format(path)
                logger.info(msg)
                tic = time.time()

            _cache._srcArrowDS = \
                ParquetDataset(
                    path_or_paths=path,
                    filesystem=
                        S3FileSystem(
                            key=aws_access_key_id,
                            secret=aws_secret_access_key)
                        if path.startswith('s3')
                        else (self._HDFS_ARROW_FS
                              if fs._ON_LINUX_CLUSTER_WITH_HDFS
                              else self._LOCAL_ARROW_FS),
                    schema=None, validate_schema=False, metadata=None,
                    split_row_groups=False)

            if verbose:
                toc = time.time()
                logger.info(msg + ' done!   <{:,.1f} s>'.format(toc - tic))

            _cache.nPieces = len(_cache._srcArrowDS.pieces)

            if _cache.nPieces:
                _cache.piecePaths = set()

                _pathPlusSepLen = len(path) + 1

                _cache.pieceSubPaths = set()

                for i, piece in enumerate(_cache._srcArrowDS.pieces):
                    piecePath = piece.path
                    _cache.piecePaths.add(piecePath)

                    pieceSubPath = piecePath[_pathPlusSepLen:]
                    _cache.pieceSubPaths.add(pieceSubPath)

                    if not i:
                        _cache._partitionedByDateOnly = \
                            pieceSubPath.startswith('{}='.format(self._DEFAULT_D_COL)) and \
                            (pieceSubPath.count('/') == 1)

            else:
                _cache.nPieces = 1
                _cache.path = os.path.dirname(path)
                _cache.piecePaths = {_cache.path}
                _cache.pieceSubPaths = {os.path.basename(path)}
                _cache._partitionedByDateOnly = False

            if path.startswith('s3'):
                _cache.s3Client = \
                    s3.client(
                        access_key_id=aws_access_key_id,
                        secret_access_key=aws_secret_access_key)

                _parsedURL = urlparse(url=path, scheme='', allow_fragments=True)
                _cache.s3Bucket = _parsedURL.netloc
                _cache.pathS3Key = _parsedURL.path[1:]

                _cache.tmpDirS3Key = self._TMP_DIR_PATH.strip('/')

                _cache.tmpDirPath = \
                    os.path.join(
                        's3://{}'.format(_cache.s3Bucket),
                        _cache.tmpDirS3Key)

                path = s3.s3a_path_with_auth(
                        s3_path=path,
                        access_key_id=aws_access_key_id,
                        secret_access_key=aws_secret_access_key)

            else:
                _cache.s3Client = _cache.s3Bucket = _cache.tmpDirS3Key = None
                _cache.tmpDirPath = self._TMP_DIR_PATH

            if not arimo.util.data_backend.chkSpark():
                arimo.util.data_backend.initSpark(sparkConf=kwargs.pop('sparkConf', {}))

            if verbose:
                msg = 'Loading {} by Spark...'.format(self.path)
                logger.info(msg)
                tic = time.time()

            _srcSparkDF = \
                arimo.util.data_backend.spark.read.load(
                    path=path,
                    format='parquet')

            _schema = _srcSparkDF.schema

            for colName in _srcSparkDF.columns:
                if _schema[colName].dataType.simpleString() == _BINARY_TYPE:
                    _srcSparkDF = \
                        _srcSparkDF.withColumn(
                            colName=colName,
                            col=_srcSparkDF[colName].astype(_STR_TYPE))

            _cache._srcSparkDF = _srcSparkDF

            _cache._srcNRows = _srcSparkDF.count()

            if verbose:
                toc = time.time()
                logger.info(msg + ' done!   <{:,.1f} s>'.format(toc - tic))

        self.__dict__.update(_cache)

        alias = kwargs.pop('alias', None)
            
        if _initSparkDF:
            super(S3ParquetDistributedDataFrame, self).__init__(
                sparkDF=_initSparkDF,
                **kwargs)

        else:
            super(S3ParquetDistributedDataFrame, self).__init__(
                sparkDF=self._srcSparkDF,
                nRows=self._srcNRows,
                **kwargs)

        self._initSparkDF = self._sparkDF

        self._sparkDFTransforms = _sparkDFTransforms

        self._pandasDFTransforms = _pandasDFTransforms

        if _sparkDF is None:
            if _sparkDFTransforms:
                for i, sparkDFTransform in enumerate(_sparkDFTransforms):
                    try:
                        self._sparkDF = \
                            sparkDFTransform.transform(dataset=self._sparkDF) \
                            if isinstance(sparkDFTransform, Transformer) \
                            else sparkDFTransform(self._sparkDF)

                    except Exception as err:
                        self.stdout_logger.error(
                            msg='*** {} TRANSFORM #{}: ***'
                                .format(self.path, i))
                        raise err

                _recacheTypes = True

            else:
                _recacheTypes = False

        else:
            self._sparkDF = _sparkDF
            _recacheTypes = True

        if alias:
            self.alias = alias

        if _recacheTypes:
            self._cache.type = \
                Namespace(**
                    {col: type
                     for col, type in self.dtypes})

        self._reprSampleMinNPieces = min(reprSampleMinNPieces, self.nPieces)

        self._cache.pieceADFs = {}

    @classmethod
    def load(cls, path, **kwargs):
        return cls(path=path, **kwargs)

    # ********************************
    # "INTERNAL / DON'T TOUCH" METHODS
    # _inplace

    def _inplace(self, adf, alias=None):
        if isinstance(adf, (tuple, list)):   # just in case we're taking in multiple inputs
            adf = adf[0]

        assert isinstance(adf, S3ParquetDistributedDataFrame)

        self.path = adf.path

        self.__dict__.update(self._CACHE[adf.path])

        self._initSparkDF = adf._initSparkDF
        self._sparkDFTransforms = adf._sparkDFTransforms
        self._pandasDFTransforms = adf._pandasDFTransforms
        self._sparkDF = adf._sparkDF

        self.alias = alias \
            if alias \
            else (self._alias
                  if self._alias
                  else adf._alias)

    # **********************
    # PYTHON DEFAULT METHODS
    # __dir__
    # __getitem__
    # __repr__
    # __short_repr__

    def __dir__(self):
        return sorted(set(
            dir(type(self)) +
            self.__dict__.keys() +
            dir(DataFrame) +
            dir(self._sparkDF)))

    def __getitem__(self, item):
        return self.transform(
                sparkDFTransform=
                    lambda sparkDF:
                        sparkDF[item],
                pandasDFTransform=_S3ParquetDataFeeder__getitem__pandasDFTransform(item=list(item)),
                inheritCache=True,
                inheritNRows=True) \
            if isinstance(item, (list, tuple)) \
          else super(S3ParquetDistributedDataFrame, self).__getitem__(item)

    def __repr__(self):
        cols = self.columns

        cols_and_types_str = []

        if self._iCol in cols:
            cols_and_types_str += ['(iCol) {}: {}'.format(self._iCol, self._cache.type[self._iCol])]

        if self._dCol in cols:
            cols_and_types_str += ['(dCol) {}: {}'.format(self._dCol, self._cache.type[self._dCol])]

        if self._tCol in cols:
            cols_and_types_str += ['(tCol) {}: {}'.format(self._tCol, self._cache.type[self._tCol])]

        cols_and_types_str += \
            ['{}: {}'.format(col, self._cache.type[col])
             for col in self.contentCols]

        return '{}{:,}-piece {}{}{}["{}" + {:,} transform(s)][{}]'.format(
                '"{}" '.format(self._alias)
                    if self._alias
                    else '',
                self.nPieces,
                '' if self._cache.nRows is None
                   else '{:,}-row '.format(self._cache.nRows),
                '(cached) '
                    if self.is_cached
                    else '',
                type(self).__name__,
                self.path,
                len(self._sparkDFTransforms),
                ', '.join(cols_and_types_str))

    @property
    def __short_repr__(self):
        cols = self.columns

        cols_desc_str = []

        if self._iCol in cols:
            cols_desc_str += ['iCol: {}'.format(self._iCol)]

        if self._dCol in cols:
            cols_desc_str += ['dCol: {}'.format(self._dCol)]

        if self._tCol in cols:
            cols_desc_str += ['tCol: {}'.format(self._tCol)]

        cols_desc_str += ['{} content col(s)'.format(len(self.contentCols))]

        return '{}{:,}-piece {}{}{}[{:,} transform(s)][{}]'.format(
                '"{}" '.format(self._alias)
                    if self._alias
                    else '',
                self.nPieces,
                '' if self._cache.nRows is None
                   else '{:,}-row '.format(self._cache.nRows),
                '(cached) '
                    if self.is_cached
                    else '',
                type(self).__name__,
                len(self._sparkDFTransforms),
                ', '.join(cols_desc_str))

    # **********
    # TRANSFORMS
    # transform
    # select
    # sql
    # __call__
    # fillna
    # prep
    # drop
    # filter
    # withColumn

    def transform(self, sparkDFTransform, _sparkDF=None, pandasDFTransform=[], *args, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        inheritCache = kwargs.pop('inheritCache', False)

        if isinstance(sparkDFTransform, list):
            additionalSparkDFTransforms = sparkDFTransform

            inheritCache |= \
                all(isinstance(additionalSparkDFTransform, Transformer)
                    for additionalSparkDFTransform in additionalSparkDFTransforms)

        elif isinstance(sparkDFTransform, Transformer):
            additionalSparkDFTransforms = [sparkDFTransform]
            inheritCache = True

        else:
            additionalSparkDFTransforms = \
                [(lambda sparkDF: sparkDFTransform(sparkDF, *args, **kwargs))
                 if args or kwargs
                 else sparkDFTransform]

        additionalPandasDFTransforms = \
            pandasDFTransform \
            if isinstance(pandasDFTransform, list) \
            else [pandasDFTransform]

        inheritNRows = kwargs.pop('inheritNRows', inheritCache)

        if _sparkDF is None:
            _sparkDF = self._sparkDF

            for i, additionalSparkDFTransform in enumerate(additionalSparkDFTransforms):
                try:
                    _sparkDF = additionalSparkDFTransform.transform(dataset=_sparkDF) \
                        if isinstance(additionalSparkDFTransform, Transformer) \
                        else additionalSparkDFTransform(_sparkDF)

                except Exception as err:
                    self.stdout_logger.error(
                        msg='*** {} ADDITIONAL TRANSFORM #{} ({}): ***'
                            .format(self.path, i, additionalSparkDFTransform))
                    raise err

        if self.s3Client:
            aws_access_key_id = self._srcArrowDS.fs.key
            aws_secret_access_key = self._srcArrowDS.fs.secret

        else:
            aws_access_key_id = aws_secret_access_key = None

        arrowDDF = \
            S3ParquetDistributedDataFrame(
                path=self.path,
                aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                _initSparkDF=self._initSparkDF,
                _sparkDFTransforms=self._sparkDFTransforms + additionalSparkDFTransforms,
                _pandasDFTransforms=self._pandasDFTransforms + additionalPandasDFTransforms,
                _sparkDF=_sparkDF,
                nRows=self._cache.nRows
                    if inheritNRows
                    else None,
                **stdKwArgs.__dict__)

        if inheritCache:
            arrowDDF._inheritCache(self)

        arrowDDF._cache.pieceADFs = self._cache.pieceADFs

        return arrowDDF

    def select(self, *exprs, **kwargs):
        if exprs:
            inheritCache = kwargs.pop('inheritCache', '*' in exprs)

        else:
            exprs = '*',
            inheritCache = kwargs.pop('inheritCache', True)

        inheritNRows = kwargs.pop('inheritNRows', inheritCache)

        return self.transform(
            sparkDFTransform=
                (lambda sparkDF: sparkDF.selectExpr(*exprs))
                if all(isinstance(expr, str) for expr in exprs)
                else (lambda sparkDF: sparkDF.select(*exprs)),
            pandasDFTransform=[],   # no Pandas equivalent
            inheritCache=inheritCache,
            inheritNRows=inheritNRows,
            **kwargs)

    def sql(self, query='SELECT * FROM this', tempAlias='this', **kwargs):
        origAlias = self._alias
        self.alias = tempAlias

        try:
            _lower_query = query.strip().lower()
            assert _lower_query.startswith('select')

            _sparkDF = arimo.util.data_backend.spark.sql(query)
            self.alias = origAlias

            inheritCache = \
                kwargs.pop(
                    'inheritCache',
                    (('select *' in _lower_query) or ('select {}.*'.format(tempAlias.lower()) in _lower_query)) and
                    ('where' not in _lower_query) and ('join' not in _lower_query) and ('union' not in _lower_query))

            inheritNRows = kwargs.pop('inheritNRows', inheritCache)

            return self.transform(
                sparkDFTransform=
                    SQLTransformer(
                        statement=
                            query.replace(' {}'.format(tempAlias), ' __THIS__')
                                 .replace('{} '.format(tempAlias), '__THIS__ ')
                                 .replace('{}.'.format(tempAlias), '__THIS__.')),
                pandasDFTransform=[],   # no Pandas equivalent
                _sparkDF=_sparkDF,
                inheritCache=inheritCache,
                inheritNRows=inheritNRows,
                **kwargs)

        except Exception as exception:
            self.alias = origAlias
            raise exception

    def __call__(self, *args, **kwargs):
        if args:
            arg = args[0]

            if isinstance(arg, Transformer) or \
                    (callable(arg) and (not isinstance(arg, DDF)) and (not isinstance(arg, types.ClassType))):
                return self.transform(
                    sparkDFTransform=arg,
                    *(args[1:]
                      if (len(args) > 1)
                      else ()),
                    **kwargs)

            elif (len(args) == 1) and isinstance(arg, str) and arg.strip().lower().startswith('select'):
                return self.sql(query=arg, **kwargs)

            else:
                return self.select(*args, **kwargs)

        else:
            return self.sql(**kwargs)

    def fillna(self, *cols, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        returnDetails = kwargs.pop('returnDetails', False)

        kwargs['returnDetails'] = \
            kwargs['returnSQLTransformer'] = True

        adf, nullFillDetails, sqlTransformer = \
            super(S3ParquetDistributedDataFrame, self).fillna(*cols, **kwargs)

        adf = self.transform(
            sparkDFTransform=sqlTransformer,
            pandasDFTransform=_S3ParquetDataFeeder__fillna__pandasDFTransform(nullFillDetails=nullFillDetails),
            _sparkDF=adf._sparkDF,
            inheritCache=True,
            inheritNRows=True,
            **stdKwArgs.__dict__)

        adf._inheritCache(adf)
        adf._cache.reprSample = self._cache.reprSample

        return (adf, nullFillDetails) \
            if returnDetails \
          else adf

    def prep(self, *cols, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        returnOrigToPrepColMaps = \
            kwargs.pop('returnOrigToPrepColMaps', False)

        kwargs['returnOrigToPrepColMaps'] = \
            kwargs['returnPipeline'] = True

        adf, catOrigToPrepColMap, numOrigToPrepColMap, pipelineModel = \
            super(S3ParquetDistributedDataFrame, self).prep(*cols, **kwargs)

        if arimo.debug.ON:
            self.stdout_logger.debug(
                msg='*** ORIG-TO-PREP METADATA: ***\n{}\n{}'
                    .format(catOrigToPrepColMap, numOrigToPrepColMap))

        adf = self.transform(
            sparkDFTransform=pipelineModel,
            pandasDFTransform=
                _S3ParquetDataFeeder__prep__pandasDFTransform(
                    addCols={},   # TODO
                    typeStrs=
                        {catCol: self._initSparkDF._schema[str(catCol)].dataType.simpleString()
                         for catCol in set(catOrigToPrepColMap).difference(('__OHE__', '__SCALE__'))},
                    catOrigToPrepColMap=catOrigToPrepColMap,
                    numOrigToPrepColMap=numOrigToPrepColMap),
            _sparkDF=adf._sparkDF,
            inheritCache=True,
            inheritNRows=True,
            **stdKwArgs.__dict__)

        adf._inheritCache(adf)
        adf._cache.reprSample = self._cache.reprSample

        return (adf, catOrigToPrepColMap, numOrigToPrepColMap) \
            if returnOrigToPrepColMaps \
          else adf

    def drop(self, *cols, **kwargs):
        return self.transform(
                sparkDFTransform=
                    lambda sparkDF:
                        sparkDF.drop(*cols),
                pandasDFTransform=_S3ParquetDataFeeder__drop__pandasDFTransform(cols=cols),
                inheritCache=True,
                inheritNRows=True,
                **kwargs)

    def filter(self, condition, **kwargs):
        return self.transform(
                sparkDFTransform=
                    lambda sparkDF:
                        sparkDF.filter(
                            condition=condition),
                pandasDFTransform=[],   # no Pandas equivalent
                inheritCache=True,
                inheritNRows=True,
                **kwargs)

    def withColumn(self, colName, colExpr, **kwargs):
        return self.transform(
                sparkDFTransform=lambda sparkDF: sparkDF.withColumn(colName=colName, col=colExpr),
                pandasDFTransform=[],   # no Pandas equivalent
                inheritCache=True,
                inheritNRows=True,
                **kwargs)

    # **************
    # SUBSET METHODS
    # _subset
    # filterByPartitionKeys
    # sample
    # gen

    def _subset(self, *pieceSubPaths, **kwargs):
        if pieceSubPaths:
            assert self.pieceSubPaths.issuperset(pieceSubPaths)

            nPieceSubPaths = len(pieceSubPaths)

            if nPieceSubPaths == self.nPieces:
                return self

            else:
                verbose = kwargs.pop('verbose', True)

                if nPieceSubPaths > 1:
                    if self.s3Client:
                        subsetDirS3Key = \
                            os.path.join(
                                self.tmpDirS3Key,
                                str(uuid.uuid4()))

                        subsetPath = \
                            os.path.join(
                                's3://{}'.format(self.s3Bucket),
                                subsetDirS3Key)

                        for pieceSubPath in \
                                (tqdm.tqdm(pieceSubPaths)
                                 if verbose
                                 else pieceSubPaths):
                            self.s3Client.copy(
                                CopySource=dict(
                                    Bucket=self.s3Bucket,
                                    Key=os.path.join(self.pathS3Key, pieceSubPath)),
                                Bucket=self.s3Bucket,
                                Key=os.path.join(subsetDirS3Key, pieceSubPath))

                        aws_access_key_id = self._srcArrowDS.fs.key
                        aws_secret_access_key = self._srcArrowDS.fs.secret

                    else:
                        subsetPath = \
                            os.path.join(
                                self.tmpDirPath,
                                str(uuid.uuid4()))

                        for pieceSubPath in \
                                (tqdm.tqdm(pieceSubPaths)
                                 if verbose
                                 else pieceSubPaths):
                            fs.cp(
                                from_path=os.path.join(self.path, pieceSubPath),
                                to_path=os.path.join(subsetPath, pieceSubPath),
                                hdfs=fs._ON_LINUX_CLUSTER_WITH_HDFS, is_dir=False)

                        aws_access_key_id = aws_secret_access_key = None

                else:
                    subsetPath = \
                        os.path.join(self.path, pieceSubPaths[0]) \
                            if self.nPieces > 1 \
                            else self.path

                    if self.s3Client:
                        aws_access_key_id = self._srcArrowDS.fs.key
                        aws_secret_access_key = self._srcArrowDS.fs.secret

                    else:
                        aws_access_key_id = aws_secret_access_key = None

                stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

                if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
                    stdKwArgs.alias = None

                adf = S3ParquetDistributedDataFrame(
                    path=subsetPath,
                    aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                    _sparkDFTransforms=self._sparkDFTransforms,
                    _pandasDFTransforms=self._pandasDFTransforms,
                    verbose=verbose,
                    **stdKwArgs.__dict__)

                adf._cache.colWidth.update(self._cache.colWidth)

                return adf

        else:
            return self

    def _pieceADF(self, pieceSubPath):
        pieceADF = self._cache.pieceADFs.get(pieceSubPath)

        if pieceADF is None:
            if self._partitionedByDateOnly:
                if self.s3Client:
                    aws_access_key_id = self._srcArrowDS.fs.key
                    aws_secret_access_key = self._srcArrowDS.fs.secret

                else:
                    aws_access_key_id = aws_secret_access_key = None

                stdKwArgs = self._extractStdKwArgs({}, resetToClassDefaults=False, inplace=False)

                if stdKwArgs.alias:
                    assert stdKwArgs.alias == self.alias
                    stdKwArgs.alias = None

                piecePath = os.path.join(self.path, pieceSubPath)

                try:
                    pieceADF = \
                        S3ParquetDistributedDataFrame(
                            path=piecePath,
                            aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                            _sparkDFTransforms=self._sparkDFTransforms,
                            _pandasDFTransforms=self._pandasDFTransforms,
                            verbose=False,
                            **stdKwArgs.__dict__)

                except Exception as err:
                    print('*** {} ***'.format(pieceSubPath))
                    raise err

                pieceADF._cache.colWidth.update(self._cache.colWidth)

            else:
                pieceADF = self._subset(pieceSubPath, verbose=False)

            self._cache.pieceADFs[pieceSubPath] = pieceADF

        else:
            pieceADF._sparkDFTransforms = sparkDFTransforms = self._sparkDFTransforms

            pieceADF._sparkDF = pieceADF._initSparkDF

            for i, sparkDFTransform in enumerate(sparkDFTransforms):
                try:
                    pieceADF._sparkDF = sparkDFTransform(pieceADF._sparkDF)

                except Exception as err:
                    self.stdout_logger.error(
                        msg='*** {} TRANSFORM #{}: ***'
                            .format(pieceSubPath, i))
                    raise err

            pieceADF._pandasDFTransforms = self._pandasDFTransforms

            pieceADF._cache.type = self._cache.type
            
        return pieceADF

    def _pieceArrowTable(self, pieceSubPath):
        return _S3ParquetDataFeeder__pieceArrowTableFunc(
                aws_access_key_id=self._srcArrowDS.fs.key,
                aws_secret_access_key=self._srcArrowDS.fs.secret)(
            os.path.join(self.path, pieceSubPath))

    def _piecePandasDF(self, pieceSubPath):
        pandasDF = \
            self._pieceArrowTable(pieceSubPath) \
                .to_pandas(
                    use_threads=max(1, psutil.cpu_count(logical=True) // 2),
                    strings_to_categorical=False,
                    memory_pool=None,
                    zero_copy_only=True)

        if self._tCol:
            pandasDF = \
                gen_aux_cols(
                    df=pandasDF,
                    i_col=self._iCol,
                    t_col=self._tCol)

        for i, pandasDFTransform in enumerate(self._pandasDFTransforms):
            try:
                pandasDF = pandasDFTransform(pandasDF)

            except Exception as err:
                self.stdout_logger.error(
                    msg='*** {}: PANDAS TRANSFORM #{} ***'
                        .format(pieceSubPath, i))
                raise err

        return pandasDF

    def filterByPartitionKeys(self, *filterCriteriaTuples, **kwargs):
        filterCriteria = {}
        _samplePieceSubPath = next(iter(self.pieceSubPaths))
        
        for filterCriteriaTuple in filterCriteriaTuples:
            assert isinstance(filterCriteriaTuple, (list, tuple))
            filterCriteriaTupleLen = len(filterCriteriaTuple)

            col = filterCriteriaTuple[0]

            if '{}='.format(col) in _samplePieceSubPath:
                if filterCriteriaTupleLen == 2:
                    fromVal = toVal = None
                    inSet = {str(v) for v in to_iterable(filterCriteriaTuple[1])}

                elif filterCriteriaTupleLen == 3:
                    fromVal = filterCriteriaTuple[1]
                    if fromVal is not None:
                        fromVal = str(fromVal)

                    toVal = filterCriteriaTuple[2]
                    if toVal is not None:
                        toVal = str(toVal)

                    inSet = None

                else:
                    raise ValueError(
                        '*** {} FILTER CRITERIA MUST BE EITHER (<colName>, <fromVal>, <toVal>) OR (<colName>, <inValsSet>) ***'
                            .format(type(self)))

                filterCriteria[col] = fromVal, toVal, inSet

        if filterCriteria:
            pieceSubPaths = set()

            for pieceSubPath in self.pieceSubPaths:
                chk = True

                for col, (fromVal, toVal, inSet) in filterCriteria.items():
                    v = re.search('{}=(.*?)/'.format(col), pieceSubPath).group(1)

                    if ((fromVal is not None) and (v < fromVal)) or \
                            ((toVal is not None) and (v > toVal)) or \
                            ((inSet is not None) and (v not in inSet)):
                        chk = False
                        break

                if chk:
                    pieceSubPaths.add(pieceSubPath)

            assert pieceSubPaths, \
                '*** {}: NO PIECE PATHS SATISFYING FILTER CRITERIA {} ***'.format(self, filterCriteria)

            if arimo.debug.ON:
                self.stdout_logger.debug(
                    msg='*** {} PIECES SATISFYING FILTERING CRITERIA: {} ***'
                        .format(len(pieceSubPaths), filterCriteria))

            return self._subset(*pieceSubPaths, **kwargs)

        else:
            return self

    def sample(self, *args, **kwargs):
        stdKwArgs = self._extractStdKwArgs(kwargs, resetToClassDefaults=False, inplace=False)

        if stdKwArgs.alias and (stdKwArgs.alias == self.alias):
            stdKwArgs.alias = None

        n = kwargs.pop('n', self._DEFAULT_REPR_SAMPLE_SIZE)
        minNPieces = kwargs.pop('minNPieces', self._reprSampleMinNPieces)
        maxNPieces = kwargs.pop('maxNPieces', None)
        verbose = kwargs.pop('verbose', True)

        sampleNPieces = \
            max(int(math.ceil(((min(n, self.nRows) / self.nRows) ** .5)
                              * self.nPieces)),
                minNPieces)

        if maxNPieces:
            sampleNPieces = min(sampleNPieces, maxNPieces)

        samplePieceSubPaths = \
            random.sample(
                population=self.pieceSubPaths,
                k=sampleNPieces) \
            if sampleNPieces < self.nPieces \
            else self.pieceSubPaths

        if verbose:
            msg = 'Sampling ~{:,} Rows from {:,} Pieces...'.format(n, sampleNPieces)
            self.stdout_logger.info(msg)

        adfs = []

        for samplePieceSubPath in \
                (tqdm.tqdm(samplePieceSubPaths)
                 if verbose
                else samplePieceSubPaths):
            try:
                adfs.append(
                    super(S3ParquetDistributedDataFrame, self._pieceADF(samplePieceSubPath))
                        .sample(n=max(n / sampleNPieces, 1), *args, **kwargs))

            except Exception as err:
                print('*** {} CANNOT BE LOADED: {} ***'.format(samplePieceSubPath, err))

        adf = DDF.unionAllCols(*adfs, **stdKwArgs.__dict__)

        adf._cache.colWidth.update(adfs[0]._cache.colWidth)

        if verbose:
            self.stdout_logger.info(msg + ' done!')

        return adf

    def gen(self, *args, **kwargs):
        if self.s3Client:
            aws_access_key_id = self._srcArrowDS.fs.key
            aws_secret_access_key = self._srcArrowDS.fs.secret

        else:
            aws_access_key_id = aws_secret_access_key = None

        return _S3ParquetDataFeeder__gen(
                args=args,
                piecePaths=kwargs.get('piecePaths', self.piecePaths),
                aws_access_key_id=aws_access_key_id, aws_secret_access_key=aws_secret_access_key,
                partitionKVs={},   # *** TODO ***
                iCol=self._iCol, tCol=self._tCol,
                possibleFeatureTAuxCols=self.possibleFeatureTAuxCols,
                contentCols=self.contentCols,
                pandasDFTransforms=self._pandasDFTransforms,
                filterConditions=kwargs.get('filter', {}),
                n=kwargs.get('n', 512),
                sampleN=kwargs.get('sampleN', 10 ** (4 if self.hasTS else 5)),
                pad=kwargs.get('pad', numpy.nan),
                anon=kwargs.get('anon', True),
                nThreads=kwargs.get('nThreads', 1))

    # ***********
    # REPR SAMPLE
    # _assignReprSample

    def _assignReprSample(self):
        adf = self.sample(
                n=self._reprSampleSize,
                minNPieces=self._reprSampleMinNPieces,
                anon=True) \
            .repartition(
                1,
                alias=(self.alias + self._REPR_SAMPLE_ALIAS_SUFFIX)
                    if self.alias
                    else None)

        adf.cache(
            eager=True,
            verbose=True)

        self._reprSampleSize = adf.nRows

        self._cache.reprSample = adf

        self._cache.nonNullProportion = {}
        self._cache.suffNonNull = {}

    # ****
    # MISC
    # split
    # copyToPath

    def split(self, *weights):
        if (not weights) or weights == (1,):
            return self

        else:
            nWeights = len(weights)
            cumuWeights = numpy.cumsum(weights) / sum(weights)

            nPieces = self.nPieces

            pieceSubPaths = list(self.pieceSubPaths)
            random.shuffle(pieceSubPaths)

            cumuIndices = \
                [0] + \
                [int(round(cumuWeights[i] * nPieces))
                 for i in range(nWeights)]

            return [self._subset(*pieceSubPaths[cumuIndices[i]:cumuIndices[i + 1]])
                    for i in range(nWeights)]

    def copyToPath(self, path, verbose=True):
        assert path.startswith('s3://')

        s3.sync(
            from_dir_path=self.path,
            to_dir_path=path,
            access_key_id=self._srcArrowDS.fs.key,
            secret_access_key=self._srcArrowDS.fs.secret,
            delete=True, quiet=True,
            verbose=verbose)
