# noinspection PyProtectedMember
from sqlalchemy.sql.base import _bind_or_error
from sqlalchemy.sql.ddl import *


class AsyncVisitor:
    async def traverse_single(self, obj, **kw):
        # noinspection PyUnresolvedReferences
        for v in self._visitor_iterator:
            meth = getattr(v, "visit_%s" % obj.__visit_name__, None)
            if meth:
                return await meth(obj, **kw)


class AsyncSchemaGenerator(AsyncVisitor, SchemaGenerator):
    async def _can_create_table(self, table):
        self.dialect.validate_identifier(table.name)
        effective_schema = self.connection.schema_for_object(table)
        if effective_schema:
            self.dialect.validate_identifier(effective_schema)
        return (not self.checkfirst or
                not (await self.dialect.has_table(self.connection,
                                                  table.name,
                                                  schema=effective_schema)))

    async def _can_create_sequence(self, sequence):
        effective_schema = self.connection.schema_for_object(sequence)

        return self.dialect.supports_sequences and \
               (
                   (not self.dialect.sequences_optional or
                    not sequence.optional) and
                   (
                       not self.checkfirst or
                       not await self.dialect.has_sequence(
                           self.connection,
                           sequence.name,
                           schema=effective_schema)
                   )
               )

    async def visit_metadata(self, metadata):
        if self.tables is not None:
            tables = self.tables
        else:
            tables = list(metadata.tables.values())

        collection = sort_tables_and_constraints(
            [t for t in tables if (await self._can_create_table(t))])

        # noinspection PyProtectedMember
        seq_coll = [s for s in metadata._sequences.values()
                    if s.column is None and await self._can_create_sequence(s)]

        event_collection = [
            t for (t, fks) in collection if t is not None
        ]
        metadata.dispatch.before_create(metadata, self.connection,
                                        tables=event_collection,
                                        checkfirst=self.checkfirst,
                                        _ddl_runner=self)

        for seq in seq_coll:
            await self.traverse_single(seq, create_ok=True)

        for table, fkcs in collection:
            if table is not None:
                await self.traverse_single(
                    table, create_ok=True,
                    include_foreign_key_constraints=fkcs,
                    _is_metadata_operation=True)
            else:
                for fkc in fkcs:
                    await self.traverse_single(fkc)

        metadata.dispatch.after_create(metadata, self.connection,
                                       tables=event_collection,
                                       checkfirst=self.checkfirst,
                                       _ddl_runner=self)

    async def visit_table(
        self, table, create_ok=False,
        include_foreign_key_constraints=None,
        _is_metadata_operation=False):
        if not create_ok and not await self._can_create_table(table):
            return

        table.dispatch.before_create(
            table, self.connection,
            checkfirst=self.checkfirst,
            _ddl_runner=self,
            _is_metadata_operation=_is_metadata_operation)

        for column in table.columns:
            if column.default is not None:
                await self.traverse_single(column.default)

        if not self.dialect.supports_alter:
            # e.g., don't omit any foreign key constraints
            include_foreign_key_constraints = None

        await self.connection.status(
            CreateTable(
                table,
                include_foreign_key_constraints=include_foreign_key_constraints
            ))

        if hasattr(table, 'indexes'):
            for index in table.indexes:
                await self.traverse_single(index)

        if self.dialect.supports_comments and not self.dialect.inline_comments:
            if table.comment is not None:
                await self.connection.status(SetTableComment(table))

            for column in table.columns:
                if column.comment is not None:
                    await self.connection.status(SetColumnComment(column))

        table.dispatch.after_create(
            table, self.connection,
            checkfirst=self.checkfirst,
            _ddl_runner=self,
            _is_metadata_operation=_is_metadata_operation)

    async def visit_foreign_key_constraint(self, constraint):
        if not self.dialect.supports_alter:
            return
        await self.connection.status(AddConstraint(constraint))

    async def visit_sequence(self, sequence, create_ok=False):
        if not create_ok and not await self._can_create_sequence(sequence):
            return
        await self.connection.status(CreateSequence(sequence))

    async def visit_index(self, index):
        await self.connection.status(CreateIndex(index))


class AsyncSchemaDropper(AsyncVisitor, SchemaDropper):
    async def visit_metadata(self, metadata):
        if self.tables is not None:
            tables = self.tables
        else:
            tables = list(metadata.tables.values())

        try:
            unsorted_tables = [t for t in tables if
                               await self._can_drop_table(t)]
            collection = list(reversed(
                sort_tables_and_constraints(
                    unsorted_tables,
                    filter_fn=lambda constraint: False
                    if not self.dialect.supports_alter
                       or constraint.name is None
                    else None
                )
            ))
        except exc.CircularDependencyError as err2:
            if not self.dialect.supports_alter:
                util.warn(
                    "Can't sort tables for DROP; an "
                    "unresolvable foreign key "
                    "dependency exists between tables: %s, and backend does "
                    "not support ALTER.  To restore at least a partial sort, "
                    "apply use_alter=True to ForeignKey and "
                    "ForeignKeyConstraint "
                    "objects involved in the cycle to mark these as known "
                    "cycles that will be ignored."
                    % (
                        ", ".join(sorted([t.fullname for t in err2.cycles]))
                    )
                )
                collection = [(t, ()) for t in unsorted_tables]
            else:
                util.raise_from_cause(
                    exc.CircularDependencyError(
                        err2.args[0],
                        err2.cycles, err2.edges,
                        msg="Can't sort tables for DROP; an "
                            "unresolvable foreign key "
                            "dependency exists between tables: %s.  Please ensure "
                            "that the ForeignKey and ForeignKeyConstraint objects "
                            "involved in the cycle have "
                            "names so that they can be dropped using "
                            "DROP CONSTRAINT."
                            % (
                                ", ".join(
                                    sorted([t.fullname for t in err2.cycles]))
                            )

                    )
                )

        seq_coll = [
            s
            for s in metadata._sequences.values()
            if s.column is None and await self._can_drop_sequence(s)
        ]

        event_collection = [
            t for (t, fks) in collection if t is not None
        ]

        metadata.dispatch.before_drop(
            metadata, self.connection, tables=event_collection,
            checkfirst=self.checkfirst, _ddl_runner=self)

        for table, fkcs in collection:
            if table is not None:
                await self.traverse_single(
                    table, drop_ok=True, _is_metadata_operation=True)
            else:
                for fkc in fkcs:
                    await self.traverse_single(fkc)

        for seq in seq_coll:
            await self.traverse_single(seq, drop_ok=True)

        metadata.dispatch.after_drop(
            metadata, self.connection, tables=event_collection,
            checkfirst=self.checkfirst, _ddl_runner=self)

    async def _can_drop_table(self, table):
        self.dialect.validate_identifier(table.name)
        effective_schema = self.connection.schema_for_object(table)
        if effective_schema:
            self.dialect.validate_identifier(effective_schema)
        return not self.checkfirst or (await self.dialect.has_table(
            self.connection, table.name, schema=effective_schema))

    async def _can_drop_sequence(self, sequence):
        effective_schema = self.connection.schema_for_object(sequence)
        return self.dialect.supports_sequences and \
               ((not self.dialect.sequences_optional or
                 not sequence.optional) and
                (not self.checkfirst or
                 await self.dialect.has_sequence(
                     self.connection,
                     sequence.name,
                     schema=effective_schema))
                )

    async def visit_index(self, index):
        await self.connection.status(DropIndex(index))

    async def visit_table(self, table, drop_ok=False,
                          _is_metadata_operation=False):
        if not drop_ok and not await self._can_drop_table(table):
            return

        table.dispatch.before_drop(
            table, self.connection,
            checkfirst=self.checkfirst,
            _ddl_runner=self,
            _is_metadata_operation=_is_metadata_operation)

        for column in table.columns:
            if column.default is not None:
                await self.traverse_single(column.default)

        await self.connection.status(DropTable(table))

        table.dispatch.after_drop(
            table, self.connection,
            checkfirst=self.checkfirst,
            _ddl_runner=self,
            _is_metadata_operation=_is_metadata_operation)

    async def visit_foreign_key_constraint(self, constraint):
        if not self.dialect.supports_alter:
            return
        await self.connection.status(DropConstraint(constraint))

    async def visit_sequence(self, sequence, drop_ok=False):
        if not drop_ok and not await self._can_drop_sequence(sequence):
            return
        await self.connection.status(DropSequence(sequence))


class GinoSchemaVisitor:
    __slots__ = ('_item',)

    def __init__(self, item):
        self._item = item

    async def create(self, bind=None, *args, **kwargs):
        if bind is None:
            bind = _bind_or_error(self._item)
        await getattr(bind, '_run_visitor')(AsyncSchemaGenerator,
                                            self._item, *args, **kwargs)
        return self._item

    async def drop(self, bind=None, *args, **kwargs):
        if bind is None:
            bind = _bind_or_error(self._item)
        await getattr(bind, '_run_visitor')(AsyncSchemaDropper,
                                            self._item, *args, **kwargs)

    async def create_all(self, bind=None, tables=None, checkfirst=True):
        await self.create(bind=bind, tables=tables, checkfirst=checkfirst)

    async def drop_all(self, bind=None, tables=None, checkfirst=True):
        await self.drop(bind=bind, tables=tables, checkfirst=checkfirst)