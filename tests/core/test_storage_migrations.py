import json
import os
import unittest

import mock
from pyramid import testing

from kinto.core.cache import postgresql as postgresql_cache
from kinto.core.permission import postgresql as postgresql_permission
from kinto.core.storage import postgresql as postgresql_storage
from kinto.core.storage.postgresql.migrator import Migrator
from kinto.core.testing import skip_if_no_postgresql


class MigratorTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.migrator = Migrator()
        migrations_directory = os.path.join(os.path.abspath(os.path.dirname(__file__)), 'migrations')
        self.migrator.migrations_directory = migrations_directory

    def test_schema_is_created_if_no_version(self):
        self.migrator.schema_version = 6
        with mock.patch.object(self.migrator, 'create_schema') as create_schema:
            self.migrator.create_or_migrate_schema()
        self.assertTrue(create_schema.called)

    def test_schema_is_not_touched_if_already_current(self):
        self.migrator.schema_version = 6
        # Patch to keep track of SQL files executed.
        with mock.patch.object(self.migrator, '_execute_sql_file') as execute_sql:
            with mock.patch.object(self.migrator, '_get_installed_version') as installed_version:
                installed_version.return_value = 6
                self.migrator.create_or_migrate_schema()
                self.assertFalse(execute_sql.called)

    def test_migration_file_is_executed_for_every_intermediary_version(self):
        self.migrator.schema_version = 6

        versions = [6, 5, 4, 3, 3]
        self.migrator._get_installed_version = lambda: versions.pop()

        with mock.patch.object(self.migrator, '_execute_sql_file') as execute_sql:
            self.migrator.create_or_migrate_schema()
        sql_called = execute_sql.call_args_list[-3][0][0]
        self.assertIn('migrations/migration_003_004.sql', sql_called)
        sql_called = execute_sql.call_args_list[-2][0][0]
        self.assertIn('migrations/migration_004_005.sql', sql_called)
        sql_called = execute_sql.call_args_list[-1][0][0]
        self.assertIn('migrations/migration_005_006.sql', sql_called)

    def test_migration_files_are_listed_if_ran_with_dry_run(self):
        self.migrator.schema_version = 6

        versions = [6, 5, 4, 3, 3]
        self.migrator._get_installed_version = lambda: versions.pop()

        with mock.patch('kinto.core.storage.postgresql.migrator.logger') as mocked:
            self.migrator.create_or_migrate_schema(dry_run=True)

        output = ''.join([repr(call) for call in mocked.info.call_args_list])
        self.assertIn('migrations/migration_003_004.sql', output)
        self.assertIn('migrations/migration_004_005.sql', output)
        self.assertIn('migrations/migration_005_006.sql', output)

    def test_migration_fails_if_intermediary_version_is_missing(self):
        self.migrator.schema_version = 6
        with mock.patch.object(self.migrator,
                               '_get_installed_version') as current:
            with mock.patch.object(self.migrator,
                                   '_execute_sql_file'):
                current.return_value = -1
                self.assertRaises(AssertionError, self.migrator.create_or_migrate_schema)


@skip_if_no_postgresql
class PostgresqlStorageMigrationTest(unittest.TestCase):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        from kinto.core.utils import sqlalchemy
        if sqlalchemy is None:
            return

        from .test_storage import PostgreSQLStorageTest
        self.settings = {**PostgreSQLStorageTest.settings}
        self.config = testing.setUp()
        self.config.add_settings(self.settings)
        self.version = postgresql_storage.Storage.schema_version
        # Usual storage object to manipulate the storage.
        self.storage = postgresql_storage.load_from_config(self.config)

    def setUp(self):
        # Start empty.
        self._delete_everything()
        # Create schema in its last version
        self.storage.initialize_schema()
        # Patch to keep track of SQL files executed.
        self.sql_execute_patcher = mock.patch(
            'kinto.core.storage.postgresql.Storage._execute_sql_file')

    def tearDown(self):
        postgresql_storage.Storage.schema_version = self.version
        mock.patch.stopall()

    def _delete_everything(self):
        q = """
        DROP TABLE IF EXISTS records CASCADE;
        DROP TABLE IF EXISTS deleted CASCADE;
        DROP TABLE IF EXISTS metadata CASCADE;
        DROP FUNCTION IF EXISTS resource_timestamp(VARCHAR, VARCHAR);
        DROP FUNCTION IF EXISTS collection_timestamp(VARCHAR, VARCHAR);
        DROP FUNCTION IF EXISTS bump_timestamp();
        """
        with self.storage.client.connect() as conn:
            conn.execute(q)

    def _load_schema(self, filepath):
        with self.storage.client.connect() as conn:
            here = os.path.abspath(os.path.dirname(__file__))
            with open(os.path.join(here, filepath)) as f:
                old_schema = f.read()
            conn.execute(old_schema)

    def test_does_not_execute_if_ran_with_dry(self):
        self._delete_everything()
        self.storage.initialize_schema(dry_run=True)
        query = """SELECT 1 FROM information_schema.tables
        WHERE table_name = 'records';"""
        with self.storage.client.connect(readonly=True) as conn:
            result = conn.execute(query)
        self.assertEqual(result.rowcount, 0)

    def test_schema_sets_the_current_version(self):
        version = self.storage._get_installed_version()
        self.assertEqual(version, self.version)

    def test_schema_is_considered_first_version_if_no_version_detected(self):
        with self.storage.client.connect() as conn:
            q = "DELETE FROM metadata WHERE name = 'storage_schema_version';"
            conn.execute(q)

        mocked = self.sql_execute_patcher.start()
        postgresql_storage.Storage.schema_version = 2
        self.storage.initialize_schema()
        sql_called = mocked.call_args[0][0]
        self.assertIn('migrations/migration_001_002.sql', sql_called)

    def test_every_available_migration(self):
        """Test every migration available in kinto.core code base since
        version 1.6.

        Records migration test is currently very naive, and should be
        elaborated along future migrations.
        """
        self._delete_everything()

        # Install old schema
        self._load_schema('schema/postgresql-storage-1.6.sql')

        # Create a sample record using some code that is compatible with the
        # schema in place in cliquet 1.6.
        with self.storage.client.connect() as conn:
            before = {'drink': 'cacao'}
            query = """
            INSERT INTO records (user_id, resource_name, data)
            VALUES (:user_id, :resource_name, (:data)::JSON)
            RETURNING id, as_epoch(last_modified) AS last_modified;
            """
            placeholders = dict(user_id='jean-louis',
                                resource_name='test',
                                data=json.dumps(before))
            result = conn.execute(query, placeholders)
            inserted = result.fetchone()
            before['id'] = str(inserted['id'])
            before['last_modified'] = inserted['last_modified']

        # In cliquet 1.6, version = 1.
        version = self.storage._get_installed_version()
        self.assertEqual(version, 1)

        # Run every migrations available.
        self.storage.initialize_schema()

        # Version matches current one.
        version = self.storage._get_installed_version()
        self.assertEqual(version, self.version)

        # Check that previously created record is still here
        migrated, count = self.storage.get_all('test', 'jean-louis')
        self.assertEqual(migrated[0], before)

        # Check that new records can be created
        r = self.storage.create('test', ',jean-louis', {'drink': 'mate'})

        # And deleted
        self.storage.delete('test', ',jean-louis', r['id'])

    def test_every_available_migration_succeeds_if_tables_were_flushed(self):
        # During tests, tables can be flushed.
        self.storage.flush()
        self.storage.initialize_schema()
        # Version matches current one.
        version = self.storage._get_installed_version()
        self.assertEqual(version, self.version)

    def test_migration_12_clean_tombstones(self):
        self._delete_everything()
        last_version = postgresql_storage.Storage.schema_version
        postgresql_storage.Storage.schema_version = 11

        self._load_schema('schema/postgresql-storage-11.sql')

        insert_query = """
        INSERT INTO records (id, parent_id, collection_id, data, last_modified)
        VALUES (:id, :parent_id, :collection_id, (:data)::JSONB, from_epoch(:last_modified))
        """
        placeholders = dict(id='rid',
                            parent_id='jean-louis',
                            collection_id='test',
                            data=json.dumps({'drink': 'mate'}),
                            last_modified=123456)
        with self.storage.client.connect() as conn:
            conn.execute(insert_query, placeholders)

        create_tombstone = """
        INSERT INTO deleted (id, parent_id, collection_id, last_modified)
        VALUES (:id, :parent_id, :collection_id, from_epoch(:last_modified))
        """
        with self.storage.client.connect() as conn:
            conn.execute(create_tombstone, placeholders)

        # Execute the 011 to 012 migration (and others)
        postgresql_storage.Storage.schema_version = last_version
        self.storage.initialize_schema()

        # Check that the rotted tombstone has been removed, but the
        # original record remains.
        records, count = self.storage.get_all('test', 'jean-louis')
        # Only the record remains.
        assert len(records) == 1
        assert count == 1

    def test_migration_18_merges_tombstones(self):
        self._delete_everything()
        last_version = postgresql_storage.Storage.schema_version

        self._load_schema('schema/postgresql-storage-11.sql')
        # Schema 11 is essentially the same as schema 17
        postgresql_storage.Storage.schema_version = 17
        with self.storage.client.connect() as conn:
            conn.execute("""
            UPDATE metadata SET value = '17'
            WHERE name = 'storage_schema_version';
            """)

        insert_query = """
        INSERT INTO records (id, parent_id, collection_id, data, last_modified)
        VALUES (:id, :parent_id, :collection_id, (:data)::JSONB, from_epoch(:last_modified))
        """
        placeholders = dict(id='rid',
                            parent_id='jean-louis',
                            collection_id='test',
                            data=json.dumps({'drink': 'mate'}),
                            last_modified=123456)
        with self.storage.client.connect() as conn:
            conn.execute(insert_query, placeholders)

        create_tombstone = """
        INSERT INTO deleted (id, parent_id, collection_id, last_modified)
        VALUES (:id, :parent_id, :collection_id, from_epoch(:last_modified))
        """
        with self.storage.client.connect() as conn:
            conn.execute(create_tombstone, placeholders)

        # Execute the 017 to 018 migration (and others)
        postgresql_storage.Storage.schema_version = last_version
        self.storage.initialize_schema()

        # Check that the record took precedence of over the tombstone.
        records, count = self.storage.get_all('test', 'jean-louis',
                                              include_deleted=True)
        assert len(records) == 1
        assert count == 1
        assert records[0]['drink'] == 'mate'


@skip_if_no_postgresql
class PostgresqlPermissionMigrationTest(unittest.TestCase):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        from kinto.core.utils import sqlalchemy
        if sqlalchemy is None:
            return

        from .test_permission import PostgreSQLPermissionTest
        settings = {**PostgreSQLPermissionTest.settings}
        config = testing.setUp()
        config.add_settings(settings)
        self.permission = postgresql_permission.load_from_config(config)

    def setUp(self):
        q = """
        DROP TABLE IF EXISTS access_control_entries CASCADE;
        DROP TABLE IF EXISTS user_principals CASCADE;
        """
        with self.permission.client.connect() as conn:
            conn.execute(q)

    def test_runs_initialize_schema_if_using_it_fails(self):
        self.permission.initialize_schema()
        query = """SELECT 1 FROM information_schema.tables
        WHERE table_name = 'user_principals';"""
        with self.permission.client.connect(readonly=True) as conn:
            result = conn.execute(query)
            self.assertEqual(result.rowcount, 1)

    def test_does_not_execute_if_ran_with_dry(self):
        self.permission.initialize_schema(dry_run=True)
        query = """SELECT 1 FROM information_schema.tables
        WHERE table_name = 'user_principals';"""
        with self.permission.client.connect(readonly=True) as conn:
            result = conn.execute(query)
        self.assertEqual(result.rowcount, 0)


@skip_if_no_postgresql
class PostgresqlCacheMigrationTest(unittest.TestCase):
    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        from kinto.core.utils import sqlalchemy
        if sqlalchemy is None:
            return

        from .test_cache import PostgreSQLCacheTest
        settings = {**PostgreSQLCacheTest.settings}
        config = testing.setUp()
        config.add_settings(settings)
        self.cache = postgresql_cache.load_from_config(config)

    def setUp(self):
        q = """
        DROP TABLE IF EXISTS cache CASCADE;
        """
        with self.cache.client.connect() as conn:
            conn.execute(q)

    def test_runs_initialize_schema_if_using_it_fails(self):
        self.cache.initialize_schema()
        query = """SELECT 1 FROM information_schema.tables
        WHERE table_name = 'cache';"""
        with self.cache.client.connect(readonly=True) as conn:
            result = conn.execute(query)
            self.assertEqual(result.rowcount, 1)

    def test_does_not_execute_if_ran_with_dry(self):
        self.cache.initialize_schema(dry_run=True)
        query = """SELECT 1 FROM information_schema.tables
        WHERE table_name = 'cache';"""
        with self.cache.client.connect(readonly=True) as conn:
            result = conn.execute(query)
        self.assertEqual(result.rowcount, 0)


class PostgresqlExceptionRaisedTest(unittest.TestCase):
    def setUp(self):
        self.sqlalchemy = postgresql_storage.client.sqlalchemy

    def tearDown(self):
        postgresql_storage.client.sqlalchemy = self.sqlalchemy

    def test_postgresql_usage_raise_an_error_if_postgresql_not_installed(self):
        postgresql_storage.client.sqlalchemy = None
        with self.assertRaises(ImportWarning):
            postgresql_storage.client.create_from_config(testing.setUp())
