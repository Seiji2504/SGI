FROM postgres:16-alpine
COPY schema.sql bootstrap.sql run-migration.sh /migrations/
RUN chmod 0555 /migrations/run-migration.sh
ENTRYPOINT ["/migrations/run-migration.sh"]
