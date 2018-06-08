# -*- coding: utf-8 -*-

import os
import pytest
import subprocess

from alembic.config import Config as AlembicConfig
from alembic.script import ScriptDirectory
from os import path
from sqlalchemy import text

import conftest

from db import db
from journalist_app import create_app

MIGRATION_PATH = path.join(path.dirname(__file__), '..', 'alembic', 'versions')

ALL_MIGRATIONS = [x.split('.')[0].split('_')[0]
                  for x in os.listdir(MIGRATION_PATH)
                  if x.endswith('.py')]


def list_migrations(cfg_path, head):
    cfg = AlembicConfig(cfg_path)
    script = ScriptDirectory.from_config(cfg)
    migrations = [x.revision
                  for x in script.walk_revisions(base='base', head=head)]
    migrations.reverse()
    return migrations


def upgrade(alembic_config, migration):
    subprocess.check_call(['alembic', 'upgrade', migration],
                          cwd=path.dirname(alembic_config))


def downgrade(alembic_config, migration):
    subprocess.check_call(['alembic', 'downgrade', migration],
                          cwd=path.dirname(alembic_config))


def get_schema(app):
    with app.app_context():
        result = list(db.engine.execute(text('''
            SELECT type, name, tbl_name, sql
            FROM sqlite_master
            ORDER BY type, name, tbl_name
            ''')))

    return {(x[0], x[1], x[2]): x[3] for x in result}


def assert_schemas_equal(left, right):
    for (k, v) in left.items():
        if k not in right:
            raise AssertionError(
                'Left contained {} but right did not'.format(k))
        if not ddl_equal(v, right[k]):
            raise AssertionError(
                'Schema for {} did not match:\nLeft:\n{}\nRight:\n{}'
                .format(k, v, right[k]))
        right.pop(k)

    if right:
        raise AssertionError(
            'Right had additional tables: {}'.format(right.keys()))


def ddl_equal(left, right):
    '''Check the "tokenized" DDL is equivalent because, because sometimes
        Alembic schemas append columns on the same line to the DDL comes out
        like:

        column1 TEXT NOT NULL, column2 TEXT NOT NULL

        and SQLAlchemy comes out:

        column1 TEXT NOT NULL,
        column2 TEXT NOT NULL
    '''
    # ignore the autoindex cases
    if left is None and right is None:
        return True

    left = [x for x in left.split() if x]
    right = [x for x in right.split() if x]
    return left == right


def test_alembic_head_matches_db_models(journalist_app,
                                        alembic_config,
                                        config):
    '''This test is to make sure that our database models in `models.py` are
       always in sync with the schema generated by `alembic upgrade head`.
    '''
    models_schema = get_schema(journalist_app)

    config.DATABASE_FILE = config.DATABASE_FILE + '.new'
    # Use the fixture to rewrite the config with the new URI
    conftest.alembic_config(config)

    # Create database file
    subprocess.check_call(['sqlite3', config.DATABASE_FILE, '.databases'])
    upgrade(alembic_config, 'head')

    # Recreate the app to get a new SQLALCHEMY_DATABASE_URI
    app = create_app(config)
    alembic_schema = get_schema(app)

    # The initial migration creates the table 'alembic_version', but this is
    # not present in the schema created by `db.create_all()`.
    alembic_schema = {k: v for k, v in alembic_schema.items()
                      if k[2] != 'alembic_version'}

    assert_schemas_equal(alembic_schema, models_schema)


@pytest.mark.parametrize('migration', ALL_MIGRATIONS)
def test_alembic_migration_upgrade(alembic_config, config, migration):
    # run migrations in sequence from base -> head
    for mig in list_migrations(alembic_config, migration):
        upgrade(alembic_config, mig)


@pytest.mark.parametrize('migration', ALL_MIGRATIONS)
def test_alembic_migration_downgrade(alembic_config, config, migration):
    # upgrade to the parameterized test case ("head")
    upgrade(alembic_config, migration)

    # run migrations in sequence from "head" -> base
    migrations = list_migrations(alembic_config, migration)
    migrations.reverse()

    for mig in migrations:
        downgrade(alembic_config, mig)


@pytest.mark.parametrize('migration', ALL_MIGRATIONS)
def test_schema_unchanged_after_up_then_downgrade(alembic_config,
                                                  config,
                                                  migration):
    # Create the app here. Using a fixture will init the database.
    app = create_app(config)

    migrations = list_migrations(alembic_config, migration)

    if len(migrations) > 1:
        target = migrations[-2]
        upgrade(alembic_config, target)
    else:
        # The first migration is the degenerate case where we don't need to
        # get the database to some base state.
        pass

    original_schema = get_schema(app)

    upgrade(alembic_config, '+1')
    downgrade(alembic_config, '-1')

    reverted_schema = get_schema(app)

    # The initial migration is a degenerate case because it creates the table
    # 'alembic_version', but rolling back the migration doesn't clear it.
    if len(migrations) == 1:
        reverted_schema = {k: v for k, v in reverted_schema.items()
                           if k[2] != 'alembic_version'}

    assert_schemas_equal(reverted_schema, original_schema)


@pytest.mark.parametrize('migration', ALL_MIGRATIONS)
def test_upgrade_with_data(alembic_config, config, migration):
    migrations = list_migrations(alembic_config, migration)
    if len(migrations) == 1:
        # Degenerate case where there is no data for the first migration
        return

    # Upgrade to one migration before the target
    target = migrations[-1]
    upgrade(alembic_config, target)

    # Dynamic module import
    mod_name = 'tests.migrations.migration_{}'.format(migration)
    mod = __import__(mod_name, fromlist=['UpgradeTester'])

    # Load the test data
    upgrade_tester = mod.UpgradeTester(config=config)
    upgrade_tester.load_data()

    # Upgrade to the target
    upgrade(alembic_config, migration)

    # Make sure it applied "cleanly" for some definition of clean
    upgrade_tester.check_upgrade()


@pytest.mark.parametrize('migration', ALL_MIGRATIONS)
def test_downgrade_with_data(alembic_config, config, migration):
    # Upgrade to the target
    upgrade(alembic_config, migration)

    # Dynamic module import
    mod_name = 'tests.migrations.migration_{}'.format(migration)
    mod = __import__(mod_name, fromlist=['DowngradeTester'])

    # Load the test data
    downgrade_tester = mod.DowngradeTester(config=config)
    downgrade_tester.load_data()

    # Downgrade to previous migration
    downgrade(alembic_config, '-1')

    # Make sure it applied "cleanly" for some definition of clean
    downgrade_tester.check_downgrade()
