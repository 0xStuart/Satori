#!/usr/bin/env python
# coding: utf-8

''' an api for reading and writing to disk '''

import shutil
import pyarrow.parquet as pq
import pandas as pd
from satori import config
import os
import pyarrow as pa
from satori.lib.apis import memory
from satori.lib.engine.structs import SourceStreamTargets

def safetify(path:str):
    if not os.path.exists(os.path.dirname(path)):
        os.makedirs(os.path.dirname(path))
            
class Api(object):
    def __init__(self, df:pd.DataFrame=None, source:str=None, stream:str=None, location:str=None, append:bool=None, ext:str='parquet'):
        self.df = df if df is not None else pd.DataFrame();
        self.source = source
        self.stream = stream
        self.location = location
        self.ext = ext

    def path(self, source:str=None, stream:str=None, permanent:bool=False):
        ''' Layer 0 get the path of a file '''
        source = source or self.source or config.defaultSource
        stream = stream or self.stream
        return os.path.join(
                self.location or config.dataPath(),
                'permanent' if permanent else 'incremental', # 'incremental', # we need a name for not permanent because what if a stream source is called permanent...
                source,
                f'{stream}.{self.ext}')

    def exists(self, source:str=None, stream:str=None, permanent:bool=False,):
        ''' Layer 0 return True if file exists at path, else False '''
        return os.path.exists(self.path(source, stream, permanent))

    def dropSourceStream(self, df:pd.DataFrame):
        ''' Layer 0 '''
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel() # source
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.droplevel() # stream
        return df

    def toTable(self, df:pd.DataFrame=None):
        ''' Layer 0 '''
        return pa.Table.from_pandas(self.dropSourceStream(df if df is not None else self.df))

    def incrementals(self, source:str=None, stream:str=None):
        ''' Layer 0 '''
        return os.listdir(self.path(source, stream))

    def append(self, df:pd.DataFrame=None):
        ''' Layer 1
        writes a dataframe to a parquet file.
        must remove multiindex column first.
        must use write_to_dataset rather than write_to_table to support append.
        streamId is the name of file.
        '''
        pq.write_to_dataset(self.toTable(df), self.path())

    def write(self, df:pd.DataFrame=None):
        ''' Layer 1
        writes a dataframe to a parquet file.
        must remove multiindex column first.
        streamId is the name of file.
        '''
        pq.write_table(self.toTable(df), self.path(permanent=True))        
        
    def compress(self, source:str=None, stream:str=None):
        ''' Layer 1
        assumes columns are always the same...
        this function is used on rare occasion to compress the on disk 
        incrementally saved data to long term storage. The compressed
        table takes up less room than the dataset because the dataset
        is partitioned into many files, allowing us to easily append
        to it. So we normally append observations to the dataset, and
        occasionally, like daily or weekly, run this compress function
        to save it to long term storage. We can still query long term
        storage the same way.
        '''
        source = source or self.source
        stream = stream or self.stream
        df = self.readBoth(source, stream)
        if df is not None:
            self.remove(source, stream, True)
            self.write(df)
            self.remove(source, stream, False)

    def remove(self, source:str=None, stream:str=None, permanent:bool=None):
        ''' Layer 1 when we don't use a stream anymore we'll remove it '''
        source = source or self.source
        stream = stream or self.stream
        if permanent is None:
            self.remove(source, stream, True)    
            self.remove(source, stream, False)    
        elif permanent:
            if self.exists(source, stream, permanent):
                os.remove(self.path(source, stream, permanent))
        else:
            shutil.rmtree(self.path(source, stream), ignore_errors=True)            
                
    def readBoth(self, source:str, stream:str, **kwargs):
        ''' Layer 1 '''
        return self.merge(
            self.read(source, stream, permanent=False, **kwargs),
            self.read(source, stream, permanent=True, **kwargs), 
            source, stream)
        
    def merge(self, df:pd.DataFrame, long:pd.DataFrame, source:str, stream:str):
        ''' Layer 1 
        meant to merge long term (permanent) written tables 
        with short term (incremental) appended datasets
        for one stream
        '''
        def dropDuplicates(df:pd.DataFrame):
            return df.drop_duplicates(subset=(source, stream, 'StreamObservationId'), keep='last').sort_index()
        
        if df is None and long is None:
            return None
        if df is None:
            return dropDuplicates(long)
        if long is None:
            return dropDuplicates(df)
        df['TempIndex'] = df.index 
        long['TempIndex'] = long.index 
        df = pd.merge(df, long, how='outer', on=list(df.columns)) 
        df.index = df['TempIndex']  
        df.index.name = None
        df = df.drop('TempIndex', axis=1, level=0) 
        return dropDuplicates(df)

    def read(self, source:str=None, stream:str=None, permanent:bool=None, **kwargs):
        ''' Layer 1
        reads a parquet file with filtering, use columns=[targets].
        adds on the stream as first level in multiindex column on dataframe.
        Since we compress incremental observations into long term storage we
        really have 2 datasets per stream to look up, thus we specify permanent
        as None in order to pull from both datasets and merge automatically.
        '''
        def conform(**kwargs):
            if 'columns' in kwargs.keys():
                if 'StreamObservationId' not in kwargs.get('columns', []):
                    kwargs['columns'].append('StreamObservationId')
                if '__index_level_0__' not in kwargs.get('columns', []):
                    kwargs['columns'].append('__index_level_0__')
            return kwargs 
        
        source = source or self.source or self.df.columns.levels[0]
        stream = stream or self.stream or self.df.columns.levels[1]
        if permanent is None:
            return self.readBoth(source, stream, **kwargs)
        if not self.exists(source, stream, permanent):
            return None
        rdf = pq.read_table(self.path(source, stream, permanent), **conform(**kwargs)).to_pandas()
        if '__index_level_0__' in rdf.columns:
            rdf.index = rdf.loc[:, '__index_level_0__']
            rdf.index.name = None
            rdf = rdf.drop('__index_level_0__', axis=1)
        rdf.columns = pd.MultiIndex.from_product([[source], [stream], rdf.columns])
        return rdf.sort_index()
    
    def gather(
        self, 
        targetColumn:'str|tuple[str]',
        targetsByStreamBySource:dict[str, dict[str, list[str]]]=None,
        targetsByStream:dict[str, list[str]]=None,
        targets:list[str]=None,
        sourceStreamTargets:list=None,
        sourceStreamTargetss:list[SourceStreamTargets]=None,
        source:str=None,
        stream:str=None,
    ):
        ''' Layer 2. 
        retrieves the targets and merges them.
        as a prime example of premature optimization I made 
        this function callable in a myriad of various ways...
        I don't remember why.
        '''
        def dropIf(df:pd.DataFrame, column:tuple):
            if df is not None:
                return df.drop(column, axis=1)
        
        def filterNone(items:list):
            return [x for x in items if x is not None]
        
        source = source or self.source
        stream = stream or self.stream
        if sourceStreamTargetss is not None:
            return memory.merge(filterNone([
                    dropIf(self.read(source, stream, columns=targets), (source, stream, 'StreamObservationId'))
                    for source, stream, targets in SourceStreamTargets.condense(sourceStreamTargetss)]),
                targetColumn=targetColumn)
        if sourceStreamTargets is not None:
            return memory.merge(filterNone([
                    dropIf(self.read(source, stream, columns=targets), (source, stream, 'StreamObservationId'))
                    for source, stream, targets in sourceStreamTargets]),
                targetColumn=targetColumn)
        if targets is not None:
            return memory.merge(
                dropIf(
                    self.read(
                        source or self.source,
                        stream or self.stream,
                        columns=targets),
                    (source, stream, 'StreamObservationId')),
                targetColumn=targetColumn)
        if targetsByStream is not None:
            return memory.merge(filterNone([
                    dropIf(self.read(source or self.source, stream, columns=targets), (source, stream, 'StreamObservationId'))
                    for stream, targets in targetsByStream.items()]),
                targetColumn=targetColumn)
        if targetsByStreamBySource is not None:
            return memory.merge(filterNone([
                    dropIf(self.read(source, stream, columns=targets), (source, stream, 'StreamObservationId'))
                    for source, values in targetsByStreamBySource.items()
                    for stream, targets in values]),
                targetColumn=targetColumn)
        return dropIf(self.read(source, stream), (source, stream, 'StreamObservationId'))
        
'''
from satori.lib.apis import disk
x = disk.Api(source='streamrSpoof', stream='simpleEURCleaned') 
df = x.read()
df
x.read(columns=['High'])
exit()

'''            