import os
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
import re
import json
import warnings
import petl as etl
from petl.compat import string_types
from petl.io.db_utils import _quote, _is_dbapi_connection
from petl.util.base import Table
from geopetl.base import SpatialQuery
from geopetl.util import parse_db_url

DEFAULT_WRITE_BUFFER_SIZE = 1000
MAX_NUM_POINTS_IN_GEOM_FOR_CHAR_CONVERSION_IN_DB = 150


def extract_table_schema(dbo, table_name, table_schema_output_path):
    db = OracleSdeDatabase(dbo)
    table = db.table(table_name)
    table.extract_table_schema(table_schema_output_path)

etl.extract_table_schema = extract_table_schema

def fromoraclesde(dbo, table_name, **kwargs):
    db = OracleSdeDatabase(dbo)
    table = db.table(table_name)

    return table.query(**kwargs)

etl.fromoraclesde = fromoraclesde

def tooraclesde(rows, dbo, table_name, srid=None, table_srid=None,
                buffer_size=DEFAULT_WRITE_BUFFER_SIZE, truncate=True):
    """
    Writes rows to database. Truncates by default.

    If table isn't registered with SDE, you must specify a to_srid.
    """

    # create db wrappers
    db = OracleSdeDatabase(dbo)
    table = db.table(table_name)

    # do we need to create the table?
    table_name_no_schema = table.name
    create = table_name_no_schema.upper() not in db.table_names
    # sample = 0 if create else None # sample whole table

    if create:
        # TODO create table if it doesn't exist
        raise NotImplementedError('Autocreate tables for Oracle SDE not currently implemented.')
    elif truncate:
        table.truncate()

    table.write(rows, srid=srid, table_srid=table_srid)

etl.tooraclesde = tooraclesde

def _tooraclesde(self, dbo, table_name, table_srid=None,
                 buffer_size=DEFAULT_WRITE_BUFFER_SIZE):
    """
    This wraps tooraclesde and adds a `self` arg so it can be attached to
    the Table class. This enables functional-style chaining.
    """
    return tooraclesde(self, dbo, table_name, table_srid=table_srid,
                       buffer_size=buffer_size)

Table.tooraclesde = _tooraclesde

def appendoraclesde(rows, dbo, table_name, srid=None, table_srid=None,
                buffer_size=DEFAULT_WRITE_BUFFER_SIZE):
    """
    Appends rows to database. Calls tooraclesde with truncate parameter set to False.
    """
    return tooraclesde(rows, dbo, table_name, srid=None, table_srid=None,
                buffer_size=DEFAULT_WRITE_BUFFER_SIZE, truncate=False)

etl.appendoraclesde = appendoraclesde

def _appendoraclesde(self, dbo, table_name, table_srid=None,
                 buffer_size=DEFAULT_WRITE_BUFFER_SIZE):
    """
    This wraps appendoraclesde and adds a `self` arg so it can be attached to
    the Table class. This enables functional-style chaining.
    """
    return appendoraclesde(self, dbo, table_name, table_srid=table_srid,
                       buffer_size=buffer_size)

Table.appendoraclesde = _appendoraclesde
################################################################################
# DB
################################################################################

class OracleSdeDatabase(object):
    """Wrapper for an Oracle SDE database connection."""

    def __init__(self, dbo, nls_lang='.UTF8'):
        """
        Arguments:

            dbo
                - a URL string (e.g. oraclesde://user:pass@host:port/db_name)
                - an Oracle connection string (e.g. user/pass@tnsname)
                - a cx_Oracle connection object
                - a callable that returns a cx_Oracle connection object

            nls_lang
                An Oracle environment variable for setting the "locale", which
                determines which encoding is used when writing to the database.
        """

        import cx_Oracle

        # set locale. this has to happen before connecting.
        os.environ['NLS_LANG'] = nls_lang

        # if dbo is a string, create connection object
        if isinstance(dbo, string_types):
            # try to parse as url
            try:
                parsed      = parse_db_url(dbo)
                database    = parsed['db_name']
                user        = parsed['user']
                password    = parsed['password']
                host        = parsed['host']
                port        = parsed['port'] or 1521

                # check the host for the __tns flag, in which case we treat it
                # as a tns name.
                if host.endswith('__tns'):
                    host = host.replace('__tns', '')
                    conn_str = '{}/{}@{}'.format(user, password, host)
                    dbo = cx_Oracle.connect(conn_str)
                # otherwise, treat it as a host
                else:
                    dsn = cx_Oracle.makedsn(host, port, database)
                    conn_str = '{}/{}@{}'.format(user, password, dsn)
                    dbo = cx_Oracle.connect(conn_str)

            # otherwise, assume it's an oracle-native connection string
            # e.g. user/pass@tns
            except ValueError:
                dbo = cx_Oracle.connect(dbo)

        # is it a callable?
        # REVIEW petl already handles db api connections?
        elif callable(dbo):
            dbo = dbo()

        # then it must be a cx_Oracle connection.
        elif not isinstance(dbo, cx_Oracle.Connection):
            msg = 'Not a valid argument to OracleSdeDatabase: {}'.format(dbo)
            raise petl.errors.ArgumentError(msg)

        self.dbo = dbo
        self.user = dbo.username

        # make a cursor for introspecting the db. not used to read/write data,
        # because petl handles that for us.
        self.cursor = dbo.cursor()

    @property
    def _user_p(self):
        """Prepare username for use in a SQL query."""
        return self.user.upper()

    def table(self, name):
        """Return a wrapped table object."""
        return OracleSdeTable(self, name)

    @property
    def users(self):
        """Return a list of user names."""
        stmt = """
            SELECT USERNAME
            FROM ALL_USERS
        """
        self.cursor.execute(stmt)
        return sorted([x[0] for x in self.cursor.fetchall()])

    @property
    def all_tables(self):
        """Returns a list of all tables, including those not belonging to the
        user. Each item is a dictionary with the table and owner."""
        stmt = """
            SELECT
                OWNER,
                TABLE_NAME
            FROM
                ALL_TABLES
        """
        self.cursor.execute(stmt)
        rows = [x for x in self.cursor.fetchall()]
        return [dict(zip(['owner', 'table_name'], x)) for x in rows]

    def tables_for_user(self, user):
        """Returns a list of OracleSdeTable objects of tables belonging to a
        user."""
        stmt = """
            SELECT TABLE_NAME
            FROM ALL_TABLES
            WHERE OWNER = :1
        """
        self.cursor.execute(stmt, (user,))
        tables = [x[0] for x in self.cursor.fetchall()]
        return sorted(self._exclude_sde_tables(tables))

    @property
    def table_names(self):
        """Return a list of sorted table names belonging to the user."""

        stmt = """
            SELECT TABLE_NAME
            FROM USER_TABLES
            WHERE
                TABLE_NAME NOT IN (
                    SELECT VIEW_NAME
                    FROM ALL_VIEWS
                    WHERE OWNER = '{}'
                )
        """.format(self._user_p)
        self.cursor.execute(stmt)
        table_names = [x[0] for x in self.cursor.fetchall()]

        # filter out sde business tables
        table_names_filtered = self._exclude_sde_tables(table_names)

        # sort
        table_names_sorted = sorted(table_names_filtered)

        return table_names_sorted

    def _exclude_sde_tables(self, tables):
        """Utility for removing SDE tables from a list of tables."""
        return [x for x in tables if
                not re.match('^S\d+_IDX\$$', x) and
                not re.match('^KEYSET_', x) and
                not re.match('SDE_LOGFILE', x)
               ]

    @property
    def tables(self):
        """Return a list of OracleSdeTable objects belonging to the user."""
        return [OracleSdeTable(self, x) for x in self.table_names]

    def _exec(self, stmt):
        """
        Convenience method for executing SQL. If the query yields results,
        return a list of dicts.
        """

        cursor = self.cursor
        cursor.execute(stmt)
        desc = [d[0] for d in cursor.description]
        return [dict(zip(desc, row)) for row in cursor]


################################################################################
# TABLE
################################################################################

FIELD_TYPE_MAP = {
    # num
    'NUMBER':       'num',
    'FLOAT':        'num',
    'LONG_STRING':  'num',

    # text
    'NCHAR':        'text',
    'CHAR':         'text',
    'NVARCHAR2':    'text',
    'VARCHAR2':     'text',
    'STRING':       'text',
    'FIXED_CHAR':   'text',
    # this is a "pseudocolumn" that appears to consist of string values
    # https://docs.oracle.com/cd/B19306_01/server.102/b14200/sql_elements001.htm#i46148
    'ROWID':        'text',

    # date
    'DATE':         'date',
    'TIMESTAMP':    'timestamp without time zone',

    # clob
    # TODO clean these up - how will they get used?
    'NCLOB':        'nclob',
    'BLOB':         'blob',
    'CLOB':         'clob',
    'RAW':          'raw',

    # sde stuff
    'ST_GEOMETRY':  'geom',
    # HACK: Nothing else in an SDE database should be using OBJECTVAR.
    # it turns out this isn't true. there are other user-defined data types in
    # the sde tables that come back as objectvar, or perhaps object.
    'OBJECTVAR':    'geom',
    # cx_Oracle 6.0 uses 'object' for geom
    'OBJECT':       'geom',
    'SP_GRID_INFO': 'other',
}

"""
TODO:
- give this a cursor property that points to the db's cursor? turns out it gets
  used a lot, so we should standardize how it's accessed.
- used parametrized queries/bind variables across the board
"""
class OracleSdeTable(object):
    def __init__(self, db, name, srid=None):
        self.db = db

        # Check for a schema
        if '.' in name:
            comps = name.split('.')
            self.schema = comps[0]
            self.name = comps[1]
        else:
            self.schema = None
            self.name = name
        self.geom_field = self._get_geom_field()
        self.geom_type = self._get_geom_type()
        self.max_num_points_in_geom = 0 if not self.geom_field else self._get_max_num_points_in_geom()

        # handle srid
        table_srid = self._get_srid()
        if table_srid and srid and table_srid != srid:
            warnings.warn('Table SRID {} does not match SRID provided ({}).'
                          'Using {}.'.format(table_srid, srid, srid))
        # elif not (table_srid or srid):
        #     raise ValueError('SRID could not be found. Please specify one '
        #                      'manually or register the table with SDE.')
        self.srid = srid or table_srid

        # TODO check if table is registered with SDE? and warn if not?

        self.objectid_field = self._get_objectid_field()

    def __repr__(self):
        return 'OracleSdeTable: {}.{}'.format(self.db._user_p, self.name)

    @property
    def metadata(self):
        stmt = """
            SELECT
                COLUMN_NAME,
                DATA_TYPE,
                DATA_LENGTH,
                NULLABLE,
                DATA_SCALE
            FROM ALL_TAB_COLS
            WHERE
                OWNER = :1 AND
                TABLE_NAME = :2 AND
                HIDDEN_COLUMN = 'NO'
            ORDER BY COLUMN_ID
        """

        cursor = self.db.cursor
        cursor.execute(stmt, (self._owner.upper(), self.name.upper(),))
        rows = cursor.fetchall()
        fields = OrderedDict()

        for row in rows:
            name = row[0].lower().replace(' ', '_')
            type_ = row[1]
            type_without_length = re.match('[A-Z0-9_]+', type_).group()
            length = row[2]
            nullable = row[3]
            scale = row[4]
            assert type_without_length in FIELD_TYPE_MAP, \
                '{} not a known field type' .format(type_)
            fields[name] = {
                'type': FIELD_TYPE_MAP[type_without_length],
                'db_type': type_,
                'length': length,
                'nullable': nullable == 'Y',
            }
            # Use scale to identiry intetger numeric types
            if type_without_length == 'NUMBER' and scale == 0:
                fields[name]['type'] = 'integer'
        return fields

    @property
    def sde_type(self):
        stmt = """
            SELECT T.NAME
            FROM
                GDB_ITEMS I,
                GDB_ITEMTYPES T
            WHERE
                I.PHYSICALNAME = :1 AND
                I.TYPE = T.UUID
        """
        cursor = self.db.cursor
        cursor.execute(stmt, (self._name_with_schema,))
        row = cursor.fetchone()
        try:
            sde_type = row[0]
        except (TypeError, IndexError):
            sde_type = None
        return sde_type

    @property
    def _owner(self):
        """Return the owner name for querying system tables. This is either
        the schema or the DB user."""
        return self.schema or self.db.user.upper()

    def _get_objectid_field(self):
        """Get the object ID field with a not-null constraint."""
        stmt = '''
            SELECT
                LOWER(COLUMN_NAME)
            FROM
                ALL_TAB_COLS
            WHERE
                UPPER(OWNER) = UPPER('{schema}') AND
                UPPER(TABLE_NAME) = UPPER('{name}') AND
                NULLABLE = 'N' AND
                COLUMN_NAME LIKE 'OBJECTID%'
        '''.format(schema=self._owner, name=self.name)
        self.db.cursor.execute(stmt)
        fields = self.db.cursor.fetchall()
        # When reading a non-spatial table, there may not be an object ID field
        if not (len(fields) == 1 and len(fields[0]) == 1):
            return None
        return fields[0][0]

    def extract_table_schema(self, table_schema_output_path):
        metadata = dict(self.metadata)
        type_map = {'num': 'numeric', 'geom': 'geometry', 'nclob': 'text', 'clob': 'text', 'blob': 'text'}
        if self.geom_field:
            metadata[self.geom_field]['geom_type'] = self.geom_type
            metadata[self.geom_field]['srid'] = self.srid
        metadata_fmt = {'fields':[]}
        for key in metadata:
            kv_fmt = {}
            kv_fmt['name'] = key
            md_type = metadata[key]['type']
            kv_fmt['type'] = type_map[md_type] if md_type in type_map else md_type
            geom_type = metadata[key].get('geom_type', '')
            srid = metadata[key].get('srid', '')
            nullable = metadata[key].get('nullable', '')
            if md_type == 'date':
                # Check if has time:
                stmt = '''
                SELECT count(*) from {table_name_with_schema} where TO_CHAR({key}, 'hh24:mi:ss') != '00:00:00' and rownum < 2               
                '''.format(table_name_with_schema=self._name_with_schema, key=key)
                self.db.cursor.execute(stmt)
                has_time = self.db.cursor.fetchone()[0]
                if has_time > 0:
                    kv_fmt['type'] = 'timestamp without time zone'
            elif geom_type:
                kv_fmt['geometry_type'] = geom_type.lower()
                if srid:
                    # if str(srid)[:4] == '3000':
                    #     srid = 2272
                    kv_fmt['srid'] = srid
            if not nullable:
                if not 'constraints' in kv_fmt:
                    kv_fmt['constraints'] = {}
                kv_fmt['constraints']['required'] = 'true'
            metadata_fmt['fields'].append(kv_fmt)
        if self.objectid_field:
            if not 'primaryKey' in metadata_fmt:
                metadata_fmt['primaryKey'] = []
            metadata_fmt['primaryKey'].append(self.objectid_field)

        with open(table_schema_output_path, 'w') as fp:
            json.dump(metadata_fmt, fp)

    def _get_geom_field(self):
        f = [field for field, desc in self.metadata.items() \
                if desc['type'] == 'geom']
        if len(f) == 0:
            return None
        elif len(f) > 1:
            raise LookupError('Multiple geometry fields')
        return f[0].lower()

    def _get_geom_type(self):
        """
        Returns the OGC geometry type for a table.

        This is complicated because SDE.ST_GeomType doesn't return anything
        when the table is empty. As a workaround, inspect the bitmasked values
        of the EFLAGS column of SDE.LAYERS to guess what geom type was
        specified at time of creation. Sometimes, however, the multipart flag
        is set to true but the actual geometries are stored as single-part.
        For now, any geometry in a multipart-enabled column will be returned as
        MULTIxxx (see `read` method for more).

        Shout-out to Vince as usual:
        http://gis.stackexchange.com/questions/193424/get-the-geometry-type-of-an-empty-arcsde-feature-class

        Note: if the table isn't registered with SDE, this will fail.
        """

        if self.geom_field is None:
            return None

        row_count_stmt = '''
            select count(*) from {}.{}
        '''.format(self._owner.upper(), self.name.upper())
        self.db.cursor.execute(row_count_stmt)
        row_count = self.db.cursor.fetchone()[0]
        # If the table isn't empty, get geom types from sde.st_geometrytype()
        if row_count > 0:
            stmt = '''select distinct sde.st_geometrytype({geom_field}) from {owner}.{table_name} WHERE SDE.ST_ISEMPTY({geom_field}) = 0 '''.format(geom_field=self.geom_field, owner=self._owner.upper(), table_name=self.name.upper())
            geom_type_response = self.db.cursor.execute(stmt)
            geom_types = []
            for geom_type in geom_type_response.fetchall():
                # geom_types.append(geom_type.replace('ST_', '').replace('MULTI', '')) # remove 'ST_' & 'MULTI' prefix
                geom_types.append(geom_type[0].replace('ST_', '')) # remove 'ST_' prefix
            geom_types = list(set(geom_types))
            if not geom_types:
                return None
            # if unique geom_type, use that:
            elif len(geom_types) == 1:
                geom_type = geom_types[0]
            # if not unique geom_type, check if different base type or just some rows are multi of same type:
            else:
                # if different types use 'geometry' as type:
                geom_type = 'geometry'

            return geom_type

        stmt = '''
            select
                bitand(eflags, 2),
                bitand(eflags, 4) + bitand(eflags, 8),
                bitand(eflags, 16),
                bitand(eflags, 262144)
            from sde.layers
            where
                owner = '{}' and
                table_name = '{}'
        '''.format(self._owner.upper(), self.name.upper())

        self.db.cursor.execute(stmt)
        r = self.db.cursor.fetchone()

        if r is None:
            return None

        point, line, polygon, multipart = r

        if point > 0:
            geom_type = 'POINT'
        elif line > 0:
            geom_type = 'LINESTRING'
        elif polygon > 0:
            geom_type = 'POLYGON'
        else:
            raise ValueError('Unknown geometry type')
        if multipart > 0:
            geom_type = 'MULTI' + geom_type
        return geom_type

    def _get_srid(self):
        if self.geom_field is None:
            return None

        stmt = """
            select s.auth_srid
            from sde.layers l
            join sde.spatial_references s
            on l.srid = s.srid
            where l.owner = '{}' and l.table_name = '{}'
        """.format(self._owner.upper(), self.name.upper())

        self.db.cursor.execute(stmt)
        row = self.db.cursor.fetchone()
        try:
            srid = row[0]
        except TypeError:
            # this is probably because the table isn't registered with sde
            # and no to_srid was provided.
            # raise LookupError('SRID could not be found. Please provide a value '
            #                   'for `to_srid`.')
            srid = None
        if not srid:
            stmt = '''
                select distinct sde.st_srid({geom_field}) as srid from {table_account}.{table_name} where sde.st_isempty({geom_field}) != 1
            '''.format(geom_field=self.geom_field, table_account=self._owner.upper(), table_name=self.name.upper())
            self.db.cursor.execute(stmt)
            row = self.db.cursor.fetchone()
            try:
                srid = row[0]
            except TypeError:
                srid = None
        if srid:
            if str(srid)[:4] == '3000':
                srid = 2272
        return srid

    def _get_max_num_points_in_geom(self):
        assert self.geom_field
        stmt = '''
            select max(sde.st_numpoints({})) from {}.{}
            '''.format(self.geom_field, self._owner.upper(), self.name.upper())
        self.db.cursor.execute(stmt)
        row = self.db.cursor.fetchone()
        try:
            max_num_points_in_geom = row[0]
        except TypeError:
            # this is probably because the table isn't registered with sde
            # or no geom field exists
            max_num_points_in_geom = 0
        return max_num_points_in_geom


    def wkt_getter(self, to_srid):
        assert self.geom_field
        geom_field_t = geom_field = self.geom_field
        # SDE.ST_Transform doesn't work when the datums differ. Unfortunately,
        # 4326 <=> 2272 is one of those. Using Shapely + PyProj for now.
        # if to_srid and to_srid != self.srid:
        #     geom_field_t = "SDE.ST_Transform({}, {})"\
        #         .format(geom_field, to_srid)
        # return "SDE.ST_AsText({}) AS {}"\
        #     .format(geom_field_t, geom_field)
        #
        # Determine if conversion of geom field from lob -> text can happen in the database or after using cx_oracle read() fct:
        #     - cx_oracle read() fct is much slower than conversion in the database
        #     - lob length must be < 4000 char limit for conversion in the datbase
        #     - therefore choose query based on max length of geom
        #     - for not use geom_type as proxy for length of geom (handle POINT geom_type conversions in the database
        #     - TODO: make determination based on max geom field length

##        if self.geom_type == 'POINT':
        if self.max_num_points_in_geom <= MAX_NUM_POINTS_IN_GEOM_FOR_CHAR_CONVERSION_IN_DB:
            return "CASE WHEN SDE.ST_ISEMPTY({}) = 1 then '' else TO_CHAR(SDE.ST_AsText({})) end AS {}" \
            .format(geom_field_t, geom_field_t, geom_field)
        else:
            return "CASE WHEN SDE.ST_ISEMPTY({}) = 1 then EMPTY_CLOB() else SDE.ST_AsText({}) end AS {}" \
                .format(geom_field_t, geom_field_t, geom_field)

    @property
    def _name_with_schema(self):
        """Returns the table name prepended with the schema name."""

        # If there's a schema we have to double quote the owner and the table
        # name, but also make them uppercase.
        if self.schema:
            return '.'.join([self.schema.upper(), self.name.upper()])
        return self.name

    @property
    def _name_with_schema_p(self):
        """Returns the table name prepended with the schema name, prepared
        for a query (quoted)."""

        # If there's a schema we have to double quote the owner and the table
        # name, but also make them uppercase.
        if self.schema:
            comps = [_quote(self.schema.upper()), self.name.upper()]
            return '.'.join(comps)
        return self.name

    @property
    def fields(self):
        return self.metadata.keys()

    @property
    def non_geom_fields(self):
        return [x for x in self.fields if x != self.geom_field]

    def query(self, **kwargs):
        return OracleSdeQuery(self.db, self, **kwargs)

    def _prepare_val(self, val, type_):
        """Prepare a value for entry into the DB."""
        if val is None:
            return None

        # TODO handle types. Seems to be working without this for most cases.
        if type_ == 'text':
            pass
        elif type_ == 'num':
            pass
        elif type_ == 'integer':
            pass
        elif type_ == 'geom':
            pass
        elif type_ == 'date':
            # Convert datetimes to ISO-8601
            if isinstance(val, datetime):
                # val = val.isoformat()
                # Force microsecond output
                val = val.strftime('%Y-%m-%dT%H:%M:%S.%f+00:00')
        elif type_ == 'nclob':
            pass
            # Cast as a CLOB object so cx_Oracle doesn't try to make it a LONG
            # var = self._c.var(cx_Oracle.NCLOB)
            # var.setvalue(0, val)
            # val = var
        else:
            raise TypeError("Unhandled type: '{}'".format(type_))
        return val

    def _prepare_geom(self, geom, srid, transform_srid=None, multi_geom=True):
        """Prepares WKT geometry by projecting and casting as necessary."""

        if geom is None:
            # TODO: should this use the `EMPTY` keyword?
            return '{} EMPTY'.format(self.geom_type)

        # Uncomment this to use write method #1 (see write function for details)
        # geom = "SDE.ST_Geometry('{}', {})".format(geom, srid)

        # Handle 3D geometries
        # TODO screen these with regex
        if 'NaN' in geom:
            geom = geom.replace('NaN', '0')
            geom = "ST_Force_2D({})".format(geom)

        # TODO this was copied over from PostGIS, but maybe Oracle can handle
        # them as-is?
        if 'CURVE' in geom or geom.startswith('CIRC'):
            geom = "ST_CurveToLine({})".format(geom)

        # Reproject if necessary
        # TODO: do this with pyproj since ST_Geometry can't
        # if transform_srid and srid != transform_srid:
        #      geom = "ST_Transform({}, {})".format(geom, transform_srid)

        if multi_geom:
            geom = 'ST_Multi({})'.format(geom)

        return geom

    @property
    def privileges(self):
        stmt = """
            SELECT
                GRANTEE,
                PRIVILEGE
            FROM ALL_TAB_PRIVS
            WHERE
                TABLE_SCHEMA = :1 AND
                TABLE_NAME = :2
        """
        cursor = self.db.cursor
        cursor.execute(stmt, (self.schema, self.name,))
        rows = cursor.fetchall()

        return [dict(zip(['grantee', 'privilege'], x)) for x in rows]

    @property
    def indexes(self):
        """Returns a map of index name => fields: []."""
        stmt = """
            SELECT
                INDEX_NAME,
                COLUMN_NAME
            FROM ALL_IND_COLUMNS
            WHERE
                TABLE_OWNER = :1 AND
                TABLE_NAME = :2
        """
        cursor = self.db.cursor
        cursor.execute(stmt, (self.schema, self.name,))
        rows = cursor.fetchall()

        indexes = {}

        for row in rows:
            name, field = row
            indexes.setdefault(name, {'fields': []})
            indexes[name]['fields'].append(field)

        return indexes


    def write(self, rows, srid=None, table_srid=None,
              buffer_size=DEFAULT_WRITE_BUFFER_SIZE):
        """
        Inserts dictionary row objects in the the database.
        Args: list of row dicts, table name, ordered field names

        Originally this formed one big insert statement with a chunks of x
        rows, but it's considerably faster to use the cx_Oracle `executemany`
        function. See methods 1 and 2 below.

        TODO: it might be faster to call NEXTVAL on the DB sequence for OBJECTID
        rather than use the SDE helper function.
        """
        # if len(rows) == 0:
        #     return

        # if table doesn't have a srid (probably because it isn't registered
        # with sde) and none was passed in, error
        # TODO some tables just don't have a srid -- this should probably go
        # somewhere else, or needs more logic
        #######################################################################
        # table_srid = table_srid or self.srid
        # if table_srid is None:
        #     raise ValueError('Table does not define an SRID. Please provide '
        #                         'a value for `table_srid` or register the '
        #                         'table with SDE.')

        # Get fields from the row because some fields from self.fields may be
        # optional, such as an autoincrementing PK.
        fields = rows.header()
        # Sort so LOB fields are at the end
        # TODO this will raise an error if the rows being passed in have
        # different fields from the destination table. We should do this more
        # gracefully.
        fields = sorted(fields, key=lambda x: 'lob' in self.metadata[x]['type'])

        table_geom_field = self.geom_field
        srid = srid or self.srid
        table_geom_type = self.geom_type if table_geom_field else None
        # row_geom_type = re.match('[A-Z]+', rows[0][geom_field]).group() \
        #     if geom_field else None

        if table_geom_field:
            # get row geom field
            first_row_view = rows.head(n=1)
            first_row_header = first_row_view.header()
            first_row = first_row_view[1]

            rows_geom_field = None
            for i, val in enumerate(first_row):
                # TODO make a function to screen for wkt-like text
                if str(val).startswith(('POINT', 'POLYGON', 'LINESTRING', 'MULTIPOLYGON')):
                    if rows_geom_field:
                        raise ValueError('Multiple geometry fields found: {}'.format(', '.join([rows_geom_field, first_row_header[i]])))
                    rows_geom_field = first_row_header[i]

            if rows_geom_field:
                geom_rows = rows.selectnotnone(rows_geom_field).records()
                geom_row = geom_rows[0]
                geom = geom_row[rows_geom_field]
                try:
                    row_geom_type = re.match('[A-Z]+', geom).group()
                # For "bytes-like objects"
                except TypeError:
                    row_geom_type = re.match(b'[A-Z]+', geom).group()

                # Do we need to cast the geometry to a MULTI type? (Assuming all rows
                # have the same geom type.)
                # if row_geom_type:
                # Check for a geom_type first, in case the table is empty.
                if row_geom_type and row_geom_type.startswith('MULTI') and \
                    not row_geom_type.startswith('MULTI'):
                    multi_geom = True
                else:
                    multi_geom = False

        # Make a map of non geom field name => type
        # TODO we might not need this since self.metadata is now mappy
        type_map = OrderedDict()
        for field in fields:
            try:
                type_map[field] = self.metadata[field]['type']
            except IndexError:
                raise ValueError('Field `{}` does not exist'.format(field))
        type_map_items = type_map.items()

        # Prepare cursor for many inserts

        # # METHOD 1: one big SQL statement. Note you also have to uncomment a
        # # line in _prepare_geom to make this work.
        # # In Oracle this looks like:
        # # INSERT ALL
        # #    INTO t (col1, col2, col3) VALUES ('val1_1', 'val1_2', 'val1_3')
        # #    INTO t (col1, col2, col3) VALUES ('val2_1', 'val2_2', 'val2_3')
        # # SELECT 1 FROM DUAL;
        # fields_joined = ', '.join(fields)
        # stmt = "INSERT ALL {} SELECT 1 FROM DUAL"

        # # We always have to pass in a value for OBJECTID (or whatever the SDE
        # # PK field is; sometimes it's something like OBJECTID_3). Check to see
        # # if the user passed in a value for object ID (not likely), otherwise
        # # hardcode the sequence incrementor into the prepared statement.
        # if self.objectid_field in fields:
        #     into_clause = "INTO {} ({}) VALUES ({{}})".format(self.name, \
        #         fields_joined)
        # else:
        #     incrementor = "SDE.GDB_UTIL.NEXT_ROWID('{}', '{}')".format(self._owner, self.name)
        #     into_clause = "INTO {} ({}, {}) VALUES ({{}}, {})".format(self.name, fields_joined, self.objectid_field, incrementor)

        # METHOD 2: executemany (not working with SDE.ST_Geometry call)
        placeholders = []

        # Create placeholders for prepared statement
        for field in fields:
            type_ = type_map[field]
            if type_ == 'geom':
                geom_placeholder = 'SDE.ST_Geometry(:{}, {})'\
                                        .format(field, self.srid)
                placeholders.append(geom_placeholder)
            elif type_ == 'date':
                # Insert an ISO-8601 timestring
                placeholders.append("TO_TIMESTAMP(:{}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF\"+00:00\"')".format(field))
            else:
                placeholders.append(':' + field)

        placeholders = [x.upper() for x in placeholders]

        # Inject the object ID field if it's missing from the supplied rows
        stmt_fields = list(fields)
        if self.objectid_field and self.objectid_field not in fields:
            stmt_fields.append(self.objectid_field)
            incrementor = "SDE.GDB_UTIL.NEXT_ROWID('{}', '{}')"\
                .format(self._owner, self.name)
            placeholders.append(incrementor)

        # get input sizes so cx_Oracle what field types to expect on executemany
        # execute this later
        c = self.db.cursor
        c.execute('select * from {} where rownum = 1'.format(self.name))
        db_types = {d[0]: d[1] for d in self.db.cursor.description}

        # Prepare statement
        placeholders_joined = ', '.join(placeholders)
        stmt_fields_joined = ', '.join(stmt_fields)
        stmt = "INSERT INTO {} ({}) VALUES ({})".format(self.name, \
            stmt_fields_joined, placeholders_joined)
        self.db.cursor.prepare(stmt)

        db_types_filtered = {x.upper(): db_types.get(x.upper()) for x in fields}
        # db_types_filtered.pop('ID')

        c.setinputsizes(**db_types_filtered)

        # Make list of value lists
        val_rows = []
        cur_stmt = stmt

        # use Record object for convenience
        rows = rows.records()

        for i, row in enumerate(rows):
            val_row = {}
            for field, type_ in type_map_items:
                if type_ == 'geom':
                    geom = row[rows_geom_field]
                    val = self._prepare_geom(geom, srid, \
                        multi_geom=multi_geom)
                    val_row[field.upper()] = val
                else:
                    val = self._prepare_val(row[field], type_)
                    # TODO: NCLOBS should be inserted via array vars
                    # if type_ == 'nclob':
                    # val_row.append(val)
                    val_row[field.upper()] = val
            val_rows.append(val_row)

            if i % buffer_size == 0:
                # execute
                self.db.cursor.executemany(None, val_rows, batcherrors=True)
                self.db.dbo.commit()

                val_rows = []
                cur_stmt = stmt
        self.db.cursor.executemany(None, val_rows, batcherrors=True)
        er = self.db.cursor.getbatcherrors()
        self.db.dbo.commit()

    def truncate(self, cascade=False):
        """Delete all rows."""
        name = self._name_with_schema_p
        stmt = "TRUNCATE TABLE {}".format(name)
        stmt += ' CASCADE' if cascade else ''
        self.db.cursor.execute(stmt)
        self.db.dbo.commit()

    @property
    def count(self):
        """Count rows."""
        # note: this didn't work with a bind variable
        stmt = "SELECT COUNT(*) FROM {}".format(self._name_with_schema)
        cursor = self.db.cursor
        cursor.execute(stmt)
        return cursor.fetchone()[0]


################################################################################
# QUERY
################################################################################

class OracleSdeQuery(SpatialQuery):
    def __init__(self,  db, table, fields=None, return_geom=True, to_srid=None,
                 where=None, limit=None, timestamp=False, geom_with_srid=False):
        self.db = db
        self.table = table
        self.fields = fields
        self.return_geom = return_geom
        self.to_srid = to_srid
        self.where = where
        self.limit = limit
        self.timestamp = timestamp
        self.geom_with_srid = geom_with_srid


    def __iter__(self):
        """Proxy iteration to core petl."""

        # form sql statement
        stmt = self.stmt()

        # get petl iterator
        dbo = self.db.dbo
        db_view = etl.fromdb(dbo, stmt)
        # unpack geoms if we need to. this is slow ¯\_(ツ)_/¯
        if self.geom_field and self.return_geom and self.table.max_num_points_in_geom > MAX_NUM_POINTS_IN_GEOM_FOR_CHAR_CONVERSION_IN_DB:
            db_view = db_view.convert(self.geom_field.upper(), 'read')

        if self.geom_with_srid and self.geom_field and self.srid:
            db_view = db_view.convert(self.geom_field.upper(), lambda g: 'SRID={srid};{g}'.format(srid=self.srid, g=g) if g not in ('', None) else '')

        # lowercase headers
        headers = db_view.header()
        db_view = etl.setheader(db_view, [x.lower() for x in headers])
        iter_fn = db_view.__iter__()

        return iter_fn

    @property
    def geom_field(self):
        return self.table.geom_field

    @property
    def srid(self):
        return self.table.srid

    def stmt(self):
        # handle fields
        fields = self.fields
        if fields is None:
            # default to non geom fields
            fields = self.table.non_geom_fields
        fields = [_quote(field.upper()) for field in fields]
        # if still no fields, try select *: TODO: revisit
        if not fields:
            fields.append('{}.*'.format(self.table._name_with_schema_p))
        # handle timestamp argument
        if self.timestamp:
            fields.append('CURRENT_TIMESTAMP as etl_read_timestamp')

        # handle geom
        geom_field = self.table.geom_field
        if geom_field and self.return_geom:
            wkt_getter = self.table.wkt_getter(self.to_srid)
            fields.append(wkt_getter)

        # form statement
        fields_joined = ', '.join(fields)
        if self.timestamp:
            stmt = 'SELECT {} FROM {}, dual'.format(fields_joined, self.table._name_with_schema_p)
        else:
            stmt = 'SELECT {} FROM {}'.format(fields_joined, self.table._name_with_schema_p)

        # where conditions
        wheres = [self.where]

        # filter empty geoms which throw a db error. these are geoms that aren't
        # null, but have no points.
        # if geom_field:
        #     wheres.append('{gf} IS NULL OR SDE.ST_NumPoints({gf}) > 0'\
        #                     .format(gf=geom_field))

        if any(wheres):
            wheres_filtered = [x for x in wheres if x and len(x) > 0]
            wheres_joined = ' AND '.join(['({})'.format(x) for x in \
                                                               wheres_filtered])
            stmt += ' WHERE {}'.format(wheres_joined)

        if self.limit:
            stmt += ' WHERE ROWNUM < {}'.format(self.limit + 1)

        return stmt
