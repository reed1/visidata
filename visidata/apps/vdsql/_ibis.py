from copy import copy
import threading
import functools
import json
import operator
import re

from contextlib import contextmanager
from urllib.parse import urlsplit, urlunsplit
from visidata import VisiData, Sheet, IndexSheet, vd, date, anytype, vlen, clipdraw, colors, stacktrace, PyobjSheet, BaseSheet, ExpectedException
from visidata import ItemColumn, AttrColumn, Column, TextSheet, asyncthread, wrapply, ColumnsSheet, UNLOADED, ExprColumn, undoAttrCopyFunc, Path

vd.option('disp_ibis_sidebar', 'pending_sql', 'which sidebar property to display')
vd.option('sql_always_count', False, 'whether to include count of total number of results')
vd.option('ibis_limit', 500, 'max number of rows to get in query')

file_backed_schemes = ['sqlite', 'duckdb']

# backends whose information_schema reports primary key constraints in the standard views
pk_constraint_backends = ['postgres', 'risingwave', 'mysql']


def vdtype_to_ibis_type(t):
    from ibis.expr import datatypes as dt
    return {
        int: dt.int,
        float: dt.float,
        date: dt.date,
        str: dt.string,
    }.get(t)


def dtype_to_vdtype(dtype):
    from ibis.expr import datatypes as dt

    try:
        if isinstance(dtype, dt.Decimal):
            if dtype.scale == 0:
                return int
            else:
                return float
        if isinstance(dtype, dt.Integer):
            return int
        if isinstance(dtype, dt.Floating):
            return float
        if isinstance(dtype, (dt.Date, dt.Timestamp)):
            return date
    except TypeError:
        # For categoricals and other pandas-defined dtypes
        pass
    return anytype


def dtype_to_formatter(dtype):
    from ibis.expr import datatypes as dt

    if isinstance(dtype, dt.JSON):
        return 'json'
    return ''


@VisiData.api
def configure_ibis(vd):
    import ibis

    vd.aggregator('collect', ibis.expr.types.AnyValue.collect, 'collect a list of values')
    ibis.options.verbose_log = vd.status
    if vd.options.debug:
        ibis.options.verbose = True

    if 'ibis_type' not in set(c.expr for c in ColumnsSheet.columns):
        ColumnsSheet.columns += [
            AttrColumn('ibis_type', type=str)
        ]


@VisiData.api
def open_vdsql(vd, p, filetype=None):
    import ibis

    vd.configure_ibis()

    if not p.is_url() and p.ext in ('ddb', 'duckdb'):
        p = Path(f'duckdb://{p}')
    elif not p.is_url() and p.ext in ('sqlite', 'sqlite3'):
        p = Path(f'sqlite://{p}')

    vs = IbisTableIndexSheet(p.base_stem, source=p, filetype=None, database_name=None,
                             ibis_conpool=IbisConnectionPool(p), sheet_type=IbisTableSheet)

    # with no database in the dsn, the table list is whatever the server defaults to; ask which database instead
    return vs.databasesSheet() if vs.needs_database else vs


vd.open_ibis = vd.open_vdsql
vd.openurl_sqlite = vd.open_vdsql


class IbisConnectionPool:
    def __init__(self, source, pool=None, total=0):
        self.source = source
        self._local = threading.local()
        self._local.connection = None

    def __copy__(self):
        return IbisConnectionPool(self.source)

    @contextmanager
    def get_conn(self):
        import ibis
        if not hasattr(self._local, 'connection') or not self._local.connection:
            self._local.connection = ibis.connect(str(self.source))
        yield self._local.connection


class IbisTableIndexSheet(IndexSheet):
    # sheet_type = IbisTableSheet  # set below
    rowtype = 'tables'  # rowdef: IbisTableSheet
    guide = '''
        # Tables
        These are the tables available on the current connection.

        - `Enter` to open the table in the current row.
        - `g Enter` to open all selected tables.
    '''
    columns = [
        AttrColumn('table', 'table_name'),
    ]
    nKeys = 1

    catalog = None       # ibis catalog (what most engines call a database); None for the connection default
    database_name = None  # ibis database (what postgres et al call a schema)

    @property
    def con(self):
        return self.ibis_conpool.get_conn()

    @property
    def needs_database(self):
        'True if source is a server url with no database named in it.'
        if not isinstance(self.source, Path) or not self.source.is_url():
            return False

        url = urlsplit(str(self.source))
        # a sqlite/duckdb url is really a local file, and has no database to choose
        if not url.netloc or url.scheme in file_backed_schemes:
            return False

        return not url.path.strip('/')

    def rawSql(self, qstr):
        with self.con as con:
            return IbisTableSheet('rawsql',
                                  ibis_conpool=self.ibis_conpool,
                                  ibis_source=qstr,
                                  source=qstr,
                                  query=con.sql(qstr))

    def iterload(self):
        with self.con as con:
            schemas = self.options.postgres_schema.split()

            # without an explicit schema list, use the connection default and skip the lookup entirely
            dbnames = [dbname
                       for dbname in con.list_databases()
                       if '*' in schemas or dbname in schemas] if schemas else []

            for dbname in dbnames or [None]:
                for tblname in con.list_tables(database=dbname):
                    vs = self.sheet_type(tblname,
                        ibis_source=self.source,
                        ibis_filetype=self.filetype,
                        ibis_conpool=self.ibis_conpool,
                        catalog=self.catalog,
                        database_name=dbname or self.database_name,
                        table_name=tblname,
                        source=self.source,
                        query=None)
                    yield vs


class IbisDatabasesSheet(Sheet):
    rowtype = 'databases'  # rowdef: str (database name)
    guide = '''
        # Databases
        These are the databases available on the current connection.

        - `Enter` to open the database in the current row.  All open sheets are
          replaced, as if that database had been given on the command line.
    '''
    columns = [
        Column('database', getter=lambda c,r: r),
        Column('is_current', getter=lambda c,r: 'Y' if r == c.sheet.current_dbname else ''),
    ]
    nKeys = 1
    current_dbname = None

    @property
    def con(self):
        return self.source.ibis_conpool.get_conn()

    def iterload(self):
        with self.con as con:
            # engines with a namespace above the schema level call that level a catalog;
            # for the rest (mysql, sqlite) the top-level namespace is the database itself
            if hasattr(con, 'list_catalogs'):
                self.current_dbname = con.current_catalog
                yield from con.list_catalogs()
            else:
                self.current_dbname = con.current_database
                yield from con.list_databases()


class IbisColumn(ItemColumn):
    @property
    def ibis_type(self):
        return self.sheet.query[self.ibis_name].type()

    def formatter_json(self, fmtstr):
        # some backends hand back JSON columns already serialised; re-dumping would double-encode them
        return lambda v, *args, **kwargs: v if isinstance(v, str) else json.dumps(v, default=str)

    @asyncthread
    def memo_aggregate(self, agg, rows):
        'Show aggregated value in status, and add ibis expr to memory for use later.'

        aggexpr = self.ibis_aggr(agg.name)  # ignore rows, do over whole query

        with self.sheet.con as con:
            aggval = con.execute(aggexpr)

        typedval = wrapply(agg.type or self.type, aggval)
        dispval = self.format(typedval)
        k = self.name+'_'+agg.name
        vd.status(f'{k}={dispval}')
        vd.memory[k] = aggval
        # store aggexpr somewhere to use in later subquery

    def expand(self, rows):
        return self.expand_struct(rows)

    def expand_struct(self, rows):
        oldexpr = self.sheet.ibis_current_expr
        struct_field = self.get_ibis_col(oldexpr)
#        if struct_field is not StructType:
#            vd.fail('vdsql can only expand Struct columns')

        struct_fields = [struct_field[name] for name in struct_field.names]
        expandedCols = super().expand(rows)   # this must go after ibis_current_expr, because it alters ibis_current_expr
        fields = []
        for ibiscol, expcol in zip(struct_fields, expandedCols):
            fields.append(ibiscol.name(expcol.name))
    #    self.sheet.query = oldexpr.drop([struct_field.get_name()]).mutate(fields)
        self.sheet.query = oldexpr.mutate(fields)
        return expandedCols


class LazyIbisColMap:
    def __init__(self, sheet, q):
        self._sheet = sheet
        self._query = q
        self._map = {col.name: col for col in sheet.columns}

    def __getitem__(self, k):
        col = self._map[k]
        return col.get_ibis_col(self._query)


class IbisTableSheet(Sheet):
    catalog = None       # ibis catalog (what most engines call a database); None for the connection default
    database_name = None  # ibis database (what postgres et al call a schema)
    table_name = None    # name of the source table within that namespace

    @property
    def con(self):
        return self.ibis_conpool.get_conn()

    @property
    def help_sidebars(self):
        'Return list of available sidebars: (text, title) callables.'
        sidebars = []
        # Add base sidebars from parent class
        sidebars.extend(super().help_sidebars)

        # Add vdsql-specific SQL sidebars
        sidebar_options = [
            ('pending_sql', 'SQL (pending)'),
            ('base_sql', 'SQL (base)'),
            ('str_current_expr', 'Ibis expr (current)'),
            ('str_pending_expr', 'Ibis expr (pending)'),
        ]

        for attr_name, title in sidebar_options:
            try:
                # Test if the attribute can be accessed
                getattr(self, attr_name)
                # Add a lambda that captures both attr_name and title
                sidebars.append(lambda a=attr_name, t=title: (getattr(self, a), t))
            except Exception:
                if self.options.debug:
                    vd.exceptionCaught()

        # Add curcol_sql if it's available
        try:
            if self.curcol_sql:
                sidebars.append(lambda: (self.curcol_sql, 'Column SQL'))
        except Exception:
            pass

        return sidebars

    def choose_sidebar(self):
        sidebars = ['base_sql', 'pending_sql', 'ibis_current_expr', 'curcol_sql', 'pending_expr']
        opts = []
        for s in sidebars:
            try:
                opts.append({'key': s, 'value':getattr(self, s)})
            except Exception as e:
                if self.options.debug:
                    vd.exceptionCaught()

        vd.options.disp_ibis_sidebar = vd.chooseOne(opts)

    @property
    def curcol_sql(self):
        expr = self.cursorCol.get_ibis_col(self.ibis_current_expr)
        if expr is not None:
            return self.ibis_expr_to_sql(expr, fragment=True)

    def ibis_expr_to_sql(self, expr, fragment=False):
        with self.con as con:
            s = con.compile(expr, pretty=True)
        return s

    @property
    def sidebar(self) -> str:
        sbtype = self.options.disp_ibis_sidebar
        if sbtype:
            txt = str(getattr(self, sbtype, ''))
            if txt:
                return f'# {sbtype}\n'+txt

    @property
    def ibis_locals(self):
        return LazyIbisColMap(self, self.query)

    def select_row(self, row):
        k = self.rowkey(row) or vd.fail('need key column to select individual rows')
        super().selectRow(row)
        self.ibis_selection.append(self.matchRowKeyExpr(row))

    def stoggle_row(self, row):
        vd.fail('cannot toggle selection of individual row in vdsql')

    def unselect_row(self, row):
        super().unselectRow(row)
        self.ibis_selection = [ self.ibis_filter & ~self.matchRowKeyExpr(row) ]

    def matchRowKeyExpr(self, row):
        import ibis
        k = self.rowkey(row) or vd.fail('need key column to select individual rows')

        return functools.reduce(operator.and_, [
            c.get_ibis_col(self.query, typed=True) == k[i]
                for i, c in enumerate(self.keyCols)
        ])

    @property
    def ibis_current_expr(self):
        return self.get_current_expr(typed=False)

    @property
    def str_current_expr(self):
        return str(self.ibis_current_expr)

    @property
    def str_pending_expr(self):
        return str(self.pending_expr)

    def get_current_expr(self, typed=False):
        q = self.query
        extra_cols = {}
        for c in self.visibleCols:
            if c not in self.columns:  # placeholder visibleCols substitutes when there are none to show
                continue
            ibis_col = c.get_ibis_col(q, typed=typed)
            if ibis_col is not None:
                extra_cols[c.name] = ibis_col
            else:
                vd.warning(f'no column {c.name}')

        if extra_cols:
            q = q.mutate(**extra_cols)

        return q

    @property
    def ibis_filter(self):
        import ibis
        selectors = [self.ibisCompileExpr(f, self.get_current_expr(typed=True)) for f in self.ibis_selection]
        if not selectors:
            return ibis.literal(True)
        return functools.reduce(operator.or_, selectors)

    @property
    def pending_expr(self):
        import ibis
        q = self.get_current_expr(typed=True)
        if self.ibis_selection:
            q = q.filter(self.ibis_filter)

        if self._ordering:
            colorder = []
            for col, rev in self._ordering:
                ibiscol = col.get_ibis_col(q)  #1856
                if rev:
                    ibiscol = ibis.desc(ibiscol)
                colorder.append(ibiscol)
            q = q.order_by(colorder)

        return q

    def ibisCompileExpr(self, expr, q):
        if isinstance(expr, str):
            return eval(expr, vd.getGlobals(), LazyIbisColMap(self, q))
        else:
            return expr

    def evalIbisExpr(self, expr):
        return eval(expr, vd.getGlobals(), self.ibis_locals)

    @property
    def base_sql(self):
        return self.sqlize(self.ibis_current_expr)

    @property
    def pending_sql(self):
        return self.sqlize(self.pending_expr)

    def sqlize(self, expr):
        if vd.options.debug:
            expr = self.withRowcount(expr)
        return self.ibis_expr_to_sql(expr)

    @property
    def substrait(self):
        from ibis_substrait.compiler.core import SubstraitCompiler
        compiler = SubstraitCompiler()
        return compiler.compile(self.ibis_current_expr)

    def withRowcount(self, q):
        if self.options.sql_always_count:
            # return q.mutate(__n__=q.count())
            return q.cross_join(q.aggregate(__n__=lambda t: t.count()))
        return q

    def beforeLoad(self):
        self.options.disp_rstatus_fmt = self.options.disp_rstatus_fmt.replace('nRows', 'countRows')
        self.options.disp_rstatus_fmt = self.options.disp_rstatus_fmt.replace('nSelectedRows', 'countSelectedRows')

    @property
    def ibis_namespace(self):
        'Return database qualifier for con.table(), or None for the connection default.'
        if not self.database_name:
            return None
        if self.catalog:
            return (self.catalog, self.database_name)
        return self.database_name

    def primaryKeyColumns(self, con):
        'Return names of the primary key columns of table_name, in key order.  Empty if the backend does not report them.'
        if con.name not in pk_constraint_backends:
            return []

        constraints = con.table('table_constraints', database='information_schema')
        usage = con.table('key_column_usage', database='information_schema')

        pk = (constraints.filter(constraints.constraint_type == 'PRIMARY KEY',
                                 constraints.table_name == self.table_name,
                                 constraints.table_schema == (self.database_name or con.current_database))
                         .join(usage, ['constraint_catalog', 'constraint_schema', 'constraint_name'])
                         .order_by('ordinal_position')
                         .select('column_name'))

        return list(con.execute(pk)['column_name'])

    @property
    def source_keycols(self):
        'Names of the primary key columns of the source table, in key order.  Queried once per sheet.'
        if self._source_keycols is None:
            if not self.table_name:
                self._source_keycols = []
            else:
                with self.con as con:
                    self._source_keycols = self.primaryKeyColumns(con)
        return self._source_keycols

    def baseQuery(self, con):
        'Return base table for {catalog}.{database_name}.{table_name}'
        import ibis
        tbl = con.table(self.table_name, database=self.ibis_namespace)
        return ibis.table(tbl.schema(), name=self.table_name,
                          catalog=self.catalog if self.database_name else None,
                          database=self.database_name)

    def iterload(self):
        with self.con as con:
            if self.query is None:
                self.query = self.baseQuery(con)

            self.reloadColumns(self.query)  # columns based on query without metadata
            self.query_result = con.execute(self.withRowcount(self.query),
                                            limit=self.options.ibis_limit or None)

            yield from self.query_result.itertuples()


    def reloadColumns(self, expr, start=1):
        # columns are reset before every load, so fall back to the source table's primary key
        keycols = {c.name: c.keycol for c in self.keyCols} or \
                  {colname: i+1 for i, colname in enumerate(self.source_keycols)}
        self._nrows_col = -1

        for i, (colname, dtype) in enumerate(expr.schema().items(), start=start):
            keycol=keycols.get(colname, 0)
            if i-start < self.nKeys:
                keycol = i+1

            if colname == '__n__':
                self._nrows_col = i
                continue

            self.addColumn(IbisColumn(colname, i,
                           type=dtype_to_vdtype(dtype),
                           formatter=dtype_to_formatter(dtype),
                           keycol=keycol,
                           ibis_name=colname))

    @property
    def countSelectedRows(self):
        return f'{self.nSelectedRows}+'

    @property
    def countRows(self):
        if self.rows is UNLOADED:
            return None
        if not self.rows or self._nrows_col < 0:
            return self.nRows
        return self.rows[0][self._nrows_col]  # __n__

    def groupBy(self, groupByCols):
        from ibis import _
        import ibis
        from ibis.expr import datatypes as dt
        aggr_cols = [groupByCols[0].ibis_col.count().name('count')]
        for c in self.visibleCols:
            aggr_cols.extend(c.ibis_aggrs)

        q = self.ibis_current_expr
        groupq = q.aggregate(aggr_cols, by=[c.ibis_col for c in groupByCols])
        try:
            win = ibis.window(order_by=ibis.NA)
        except ibis.common.exceptions.IbisTypeError: # ibis bug: there is not yet a good workaround that covers all backends
            win = ibis.window(order_by=None)
        groupq = groupq.mutate(percent=_['count']*100 / _['count'].sum().over(win))

        histolen = 40
        histogram_char = self.options.disp_histogram
        if histogram_char and len(aggr_cols) == 1:
            groupq = groupq.mutate(maxcount=_['count'].max())
            hval = ibis.literal(histogram_char, type=dt.string)

            def _histogram(t):
                return hval.repeat((histolen*t['count']/t.maxcount).cast(dt.int))

            groupq = groupq.mutate(histogram=_histogram)

        groupq = groupq.order_by(ibis.desc('count'))

        return IbisFreqTable(self.name, *(col.name for col in groupByCols), 'freq',
                             ibis_conpool=self.ibis_conpool,
                             ibis_source=self.ibis_source,
                             source=self,
                             groupByCols=groupByCols,
                             query=groupq,
                             nKeys=len(groupByCols))

    def unfurl_col(self, col):
        vs = copy(self)
        vs.names = [self.name, col.name, 'unfurled']
        vs.query = self.ibis_current_expr.mutate(**{col.name:col.ibis_col.unnest()})
        vs.cursorVisibleColIndex = self.cursorVisibleColIndex
        return vs

    def openJoin(self, others, jointype=''):
        sheets = [self] + others

        sheets[1:] or vd.fail("join requires more than 1 sheet")

        if jointype == 'append':
            q = self.ibis_current_expr
            for other in others:
                q = q.union(other.ibis_current_expr)
            return IbisTableSheet('&'.join(vs.name for vs in sheets), query=q, ibis_source=self.ibis_source, ibis_conpool=self.ibis_conpool)

        for s in sheets:
            s.keyCols or vd.fail(f'{s.name} has no key cols to join')

        if jointype in ['extend', 'outer']:
            jointype = 'left'
        elif jointype in ['full']:
            jointype = 'outer'
#        elif jointype in ['inner']:
#            jointype = 'inner'


        q = self.ibis_current_expr
        for other in others:
            preds = [(a.ibis_col == b.ibis_col) for a, b in zip(self.keyCols, other.keyCols)]
            # Try new API (ibis >= 9.0) with lname/rname, fall back to old API with suffixes
            try:
                q = q.join(other.ibis_current_expr, predicates=preds, how=jointype, lname='', rname='{name}_'+other.name)
            except TypeError:
                # Fall back to old API (ibis < 9.0)
                q = q.join(other.ibis_current_expr, predicates=preds, how=jointype, suffixes=('', '_{name}_'+other.name))

        return IbisTableSheet('+'.join(vs.name for vs in sheets), sources=sheets, query=q, ibis_source=self.ibis_source, ibis_conpool=self.ibis_conpool)


@Column.property
def ibis_col(col):
    return col.get_ibis_col(col.sheet.ibis_current_expr)


@Column.api
def get_ibis_col(col, query:'ibis.Expr', typed=False) -> 'ibis.Expr':
    'Return ibis.Expr for `col` within context of `query`, cast by VisiData column type if `typed`.'
    import ibis.common.exceptions

    r = None
    if isinstance(col, ExprColumn):
        r = col.sheet.evalIbisExpr(col.expr)
    elif isinstance(col, vd.ExpandedColumn):
        r = query[col.name]
    elif not hasattr(col, 'ibis_name'):
        return
    else:
        try:
            r = query[col.ibis_name]
        except (ibis.common.exceptions.IbisTypeError, AttributeError):
            r = query[col.name]

    if r is None:
        return r

    if typed:
        import ibis.expr.datatypes as dt
        if col.type is str: r = r.cast(dt.string)
        if col.type is int: r = r.cast(dt.int)
        if col.type is float: r = r.cast(dt.float)
        if col.type is date:
            if not isinstance(r.type(), (dt.Timestamp, dt.Date)):
                r = r.cast(dt.date)

    r = r.name(col.name)
    return r


@Column.property
def ibis_aggrs(col):
    return [col.ibis_aggr(aggname) for aggname in (col.aggstr or '').split()]


@Column.api
def ibis_aggr(col, aggname):
    aggname = {
        'avg': 'mean',
        'median': 'approx_median',
        'mode': 'notimpl',
        'distinct': 'nunique',
        'list': 'collect',
        'stdev': 'std',
#        'p99': 'quantile(0.99)',
#        'q10': 'quantile([.1,.2,.3,.4,.5,.6,.7,.8,.9])',
    }.get(aggname, aggname)

    agg = getattr(col.ibis_col, aggname)
    return agg().name(f'{aggname}_{col.name}')


IbisTableSheet.init('ibis_selection', list, copy=False)
IbisTableSheet.init('_source_keycols', lambda: None, copy=True)
IbisTableSheet.init('_sqlscr', lambda: None, copy=False)
IbisTableSheet.init('query_result', lambda: None, copy=False)
IbisTableSheet.init('ibis_conpool', lambda: None, copy=True)

@IbisTableSheet.api
def stoggle_rows(sheet):
    sheet.toggle(sheet.rows)
    sheet.ibis_selection = [~sheet.ibis_filter]


@IbisTableSheet.api
def clearSelected(sheet):
    super(IbisTableSheet, sheet).clearSelected()
    sheet.ibis_selection.clear()



@IbisTableSheet.api
def addUndoSelection(sheet):
    super(IbisTableSheet, sheet).addUndoSelection()
    vd.addUndo(undoAttrCopyFunc([sheet], 'ibis_selection'))


@IbisTableSheet.api
def select_equal_cell(sheet, col, typedval):
    if sheet.isNullFunc()(typedval):
        expr = col.ibis_col.isnull()
    else:
        q = sheet.get_current_expr(typed=True)
        ibis_col = col.get_ibis_col(q, typed=True)
        expr = (ibis_col == typedval)

    sheet.ibis_selection.append(expr)
    sheet.select(sheet.gatherBy(lambda r,c=col,v=typedval: c.getTypedValue(r) == v), progress=False)


@IbisTableSheet.api
def select_col_regex(sheet, col, regex):
    sheet.selectByIdx(vd.searchRegex(sheet, regex=regex, columns="cursorCol"))
    sheet.ibis_selection.append(col.get_ibis_col(col.sheet.query).re_search(regex))


@IbisTableSheet.api
def select_expr(sheet, expr):
    sheet.select(sheet.gatherBy(lambda r, sheet=sheet, expr=expr: sheet.evalExpr(expr, r)), progress=False)
    sheet.ibis_selection.append(expr)


@IbisTableSheet.api
def addcol_split(sheet, col, delim):
    from ibis.expr import datatypes as dt
    c = Column(col.name+'_split',
               getter=lambda col,row: col.origCol.getDisplayValue(row).split(col.expr),
               expr=delim,
               origCol=col,
               ibis_name=col.name+'_split')
    sheet.query = sheet.query.mutate(**{c.name:col.get_ibis_col(sheet.query).cast(dt.string).split(delim)})
    return c


@IbisTableSheet.api
def addcol_subst(sheet, col, before='', after=''):
    c = Column(col.name + "_re",
               getter=lambda col,row,before=before,after=after: re.sub(before, after, col.origCol.getDisplayValue(row)),
               origCol=col,
               ibis_name=col.name + "_re")
    sheet.query = sheet.query.mutate(**{c.name:col.get_ibis_col(sheet.query).re_replace(before, after)})
    return c


@IbisTableSheet.api
def addcol_cast(sheet, col):
    # sheet.query and sheet.ibis_current_expr don't match
    new_type = vdtype_to_ibis_type(col.type)
    if new_type is None:
        vd.warning(f"no type for vd type {col.type}")
        return
    expr = sheet.query[col.name].cast(new_type)
    sheet.query = sheet.query.mutate(**{col.name: expr})
    newcol = copy(col)
    col.hide()
    sheet.addColumnAtCursor(newcol)

# disable not implemented commands

@BaseSheet.api
def notimpl(sheet):
    vd.fail(f"{vd.activeCommand.longname} not implemented for {type(sheet).__name__}; copy to new non-ibis sheet with g'")


dml_cmds = '''addcol-bulk addcol-new add-row add-rows
copy-cell copy-cells copy-row copy-selected commit-sheet cut-cell cut-cells cut-row cut-selected delete-cell delete-cells delete-row delete-selected
edit-cell paste-after paste-before paste-cell setcell-expr
setcol-clipboard setcol-expr setcol-fake setcol-fill setcol-format-enum setcol-formatter setcol-incr setcol-incr-step setcol-input setcol-iter setcol-subst setcol-subst-all
'''.split()

neverimpl_cmds = '''
select-after select-around-n select-before select-equal-row select-error stoggle-after stoggle-before stoggle-row unselect-after unselect-before select-cols-regex unselect-cols-regex transpose
'''.split()

notimpl_cmds = '''
addcol-capture addcol-incr addcol-incr-step addcol-window
contract-col expand-col-depth expand-cols expand-cols-depth melt melt-regex pivot random-rows
select-error-col select-exact-cell select-exact-row select-rows
describe-sheet freq-summary
cache-col cache-cols
dive-selected-cells
dup-rows dup-rows-deep dup-selected-deep
'''.split()

for longname in list(notimpl_cmds) + list(neverimpl_cmds) + list(dml_cmds):
    if longname:
        IbisTableSheet.addCommand('', longname, 'notimpl()')

@IbisTableSheet.api
def dup_selected(sheet):
    vs=copy(sheet)
    vs.query=sheet.pending_expr
    vs.incrementName()
    vd.push(vs)


@BaseSheet.api
def incrementName(sheet):
    if isinstance(sheet.names[-1], int):
        sheet.names[-1] += 1
    else:
        sheet.names = list(sheet.names) + [1]
    sheet.name = '_'.join(map(str, sheet.names))


@IbisTableSheet.api
def dup_limit(sheet, limit:int):
    vs=copy(sheet)
    vs.name += f"_top{limit}" if limit else "_all"
    vs.query=sheet.pending_expr
    vs.options.ibis_limit=limit
    return vs

@IbisTableSheet.api
def rawSql(sheet, qstr):
    with sheet.con as con:
        return IbisTableSheet('rawsql',
                          ibis_conpool=sheet.ibis_conpool,
                          ibis_source=qstr,
                          source=qstr,
                          query=con.sql(qstr))

@IbisTableIndexSheet.api
def databasesSheet(sheet):
    return IbisDatabasesSheet(sheet.name, 'databases', source=sheet)


@IbisTableIndexSheet.api
def sourceForDatabase(sheet, dbname:str) -> Path:
    'Return source Path for connecting to *dbname* on the same server as this sheet.'
    if not isinstance(sheet.source, Path) or not sheet.source.is_url():
        vd.fail('can only change database for a connection url')

    url = urlsplit(str(sheet.source))
    # a sqlite/duckdb url is really a local file, so there is no other database to connect to
    if not url.netloc or url.scheme in file_backed_schemes:
        vd.fail(f'can only change database for a server connection, not {url.scheme}')

    # the database is the first path segment; keep anything after it (like a snowflake schema)
    rest = url.path.split('/')[2:]
    return Path(urlunsplit(url._replace(path='/'.join(['', dbname] + rest))))


@IbisTableIndexSheet.api
def set_database(sheet, dbname:str):
    'Reconnect to *dbname*, replacing all open sheets as if it had been opened from the command line.'
    src = sheet.sourceForDatabase(dbname)
    vs = type(sheet)(src.base_stem, source=src, filetype=sheet.filetype,
                     ibis_conpool=IbisConnectionPool(src),
                     sheet_type=sheet.sheet_type)

    for oldsheet in list(vd.sheets):
        vd.remove(oldsheet)

    vd.push(vs)


class IbisFreqTable(IbisTableSheet):
    def freqExpr(self, row):
        # matching key of grouped columns
        return functools.reduce(operator.and_, [
            c.get_ibis_col(self.source.query, typed=True) == self.rowkey(row)[i]
                for i, c in enumerate(self.groupByCols)
        ])

    def selectRow(self, row):
        super().selectRow(row)
        self.source.select(self.gatherBy(lambda r, sheet=self, expr=self.freqExpr(row): sheet.evalExpr(expr, r)), progress=False)
        self.source.ibis_selection.append(self.freqExpr(row))

    def openRow(self, row):
        vs = copy(self.source)
        vs.names = list(vs.names) + ['_'.join(str(x) for x in self.rowkey(row))]
        vs.query = self.source.query.filter(self.freqExpr(row))
        return vs

    def openRows(self, rows):
        'Return sheet with union of all selected items.'
        vs = copy(self.source)
        vs.names = list(vs.names) + ['several']

        vs.query = self.source.query.filter([
            functools.reduce(operator.or_, [self.freqExpr(row) for row in rows])
        ])
        return vs


IbisTableSheet.addCommand('F', 'freq-col', 'vd.push(groupBy([cursorCol]))')
IbisTableSheet.addCommand('gF', 'freq-keys', 'vd.push(groupBy(keyCols))')

IbisTableSheet.addCommand('"', 'dup-selected', 'vd.push(dup_selected())', 'open duplicate sheet with selected rows (default limit)')
IbisTableSheet.addCommand('z"', 'dup-limit', 'vd.push(dup_limit(input("max rows: ", value=options.ibis_limit)))', 'open duplicate sheet with only selected rows (input limit)')
IbisTableSheet.addCommand('gz"', 'dup-nolimit', 'vd.push(dup_limit(0))', 'open duplicate sheet with only selected rows (no limit--be careful!)')

IbisTableSheet.addCommand("'", 'addcol-cast', 'addcol_cast(cursorCol)')

IbisTableSheet.addCommand('zb', 'sidebar-choose', 'choose_sidebar()', 'choose vdsql sidebar to show')
IbisTableSheet.addCommand('', 'exec-sql', 'vd.push(rawSql(input("SQL query: ")))', 'open sheet with results of raw SQL query')
IbisTableSheet.addCommand('', 'addcol-subst', 'addColumnAtCursor(addcol_subst(cursorCol, **inputRegexSubstOld("transform column by regex: ")))') # deprecated
IbisTableSheet.addCommand('', 'addcol-regex-subst', 'addColumnAtCursor(addcol_subst(cursorCol, **inputRegexSubst("transform column by regex: ")))')
IbisTableSheet.addCommand('', 'addcol-split', 'addColumnAtCursor(addcol_split(cursorCol, input("split by delimiter: ", type="delim-split")))')
IbisTableSheet.addCommand('gt', 'stoggle-rows', 'stoggle_rows()', 'select rows matching current cell in current column')
IbisTableSheet.addCommand(',', 'select-equal-cell', 'select_equal_cell(cursorCol, cursorTypedValue)', 'select rows matching current cell in current column')
IbisTableSheet.addCommand('t', 'stoggle-row', 'stoggle_row(cursorRow); cursorDown(1)', 'toggle selection of current row')
IbisTableSheet.addCommand('s', 'select-row', 'select_row(cursorRow); cursorDown(1)', 'select current row')
IbisTableSheet.addCommand('u', 'unselect-row', 'unselect_row(cursorRow); cursorDown(1)', 'unselect current row')
#IbisTableSheet.addCommand('g,', 'select-equal-row', 'select(gatherBy(lambda r,currow=cursorRow,vcols=visibleCols: all([c.getDisplayValue(r) == c.getDisplayValue(currow) for c in vcols])), progress=False)', 'select rows matching current row in all visible columns')
#IbisTableSheet.addCommand('z,', 'select-exact-cell', 'select(gatherBy(lambda r,c=cursorCol,v=cursorTypedValue: c.getTypedValue(r) == v), progress=False)', 'select rows matching current cell in current column')
#IbisTableSheet.addCommand('gz,', 'select-exact-row', 'select(gatherBy(lambda r,currow=cursorRow,vcols=visibleCols: all([c.getTypedValue(r) == c.getTypedValue(currow) for c in vcols])), progress=False)', 'select rows matching current row in all visible columns')

IbisTableSheet.addCommand('', 'select-col-regex', 'select_col_regex(cursorCol, inputRegex("select regex: ", type="regex", defaultLast=True))', 'select rows matching regex in current column')

IbisTableSheet.addCommand('z|', 'select-expr', 'expr=inputExpr("select by expr: "); select_expr(expr)', 'select rows matching Python expression in any visible column')
IbisTableSheet.addCommand('z\\', 'unselect-expr', 'expr=inputExpr("unselect by expr: "); unselect(gatherBy(lambda r, sheet=sheet, expr=expr: sheet.evalExpr(expr, r)), progress=False)', 'unselect rows matching Python expression in any visible column')

IbisFreqTable.addCommand('gEnter', 'open-selected', 'vd.push(openRows(selectedRows))')
IbisTableIndexSheet.addCommand('', 'exec-sql', 'vd.push(rawSql(input("SQL query: ")))', 'open sheet with results of raw SQL query')
IbisTableIndexSheet.addCommand('o', 'open-databases', 'vd.push(databasesSheet())', 'open list of databases on this connection')
IbisTableIndexSheet.addCommand('', 'set-database', 'set_database(input("database: "))', 'reconnect to given database, replacing all open sheets')
IbisDatabasesSheet.addCommand('Enter', 'set-database', 'source.set_database(cursorRow)', 'reconnect to database in current row, replacing all open sheets')

IbisTableIndexSheet.class_options.postgres_schema = ''
IbisTableIndexSheet.class_options.load_lazy = True
IbisTableIndexSheet.sheet_type = IbisTableSheet
IbisTableSheet.class_options.clean_names = True
IbisTableSheet.class_options.regex_flags = ''

vd.addMenuItem('View', 'Sidebar', 'choose', 'sidebar-choose')
