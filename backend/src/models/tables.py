from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
revision_meta = Table('revision_meta', metadata, Column('dataset_id', Integer, primary_key=True),
                      Column('kind', String(16), nullable=False), Column('base_id', Integer, nullable=True, index=True),
                      Column('seq', Integer, nullable=False, default=1), Column('created', Double, nullable=False))
base_versions = Table('base_versions', metadata, Column('base_id', Integer, primary_key=True),
                      Column('latest_dataset_id', Integer, nullable=False), Column('latest_seq', Integer, nullable=False),
                      Column('active_dataset_id', Integer, nullable=True), Column('active_seq', Integer, nullable=False),
                      Column('active_run_id', Integer, nullable=True), Column('updated', Double, nullable=False))
supplement_links = Table('supplement_links', metadata, Column('id', Integer, primary_key=True),
                         Column('base_id', Integer, nullable=False, index=True),
                         Column('supplement_id', Integer, nullable=False, index=True),
                         Column('created', Double, nullable=False), UniqueConstraint('base_id', 'supplement_id'))
merge_reports = Table('merge_reports', metadata, Column('id', Integer, primary_key=True),
                      Column('base_id', Integer, nullable=False, index=True),
                      Column('merged_id', Integer, nullable=True, index=True), Column('seq', Integer, nullable=False),
                      Column('report', JSON, nullable=False), Column('created', Double, nullable=False))
approval_pins = Table('approval_pins', metadata, Column('run_id', Integer, primary_key=True),
                      Column('dataset_id', Integer, nullable=False),
                      Column('version', Integer, nullable=False), Column('approved', Double, nullable=False))
runs = Table('analysis_runs', metadata, Column('id', Integer, primary_key=True),
             Column('dataset_id', Integer, nullable=False, index=True), Column('options', JSON, nullable=False),
             Column('state', String(16), nullable=False, index=True), Column('owner', String(40), nullable=False),
             Column('created', Double, nullable=False), Column('started', Double), Column('finished', Double),
             Column('result', JSON), Column('error', Text), Column('decision', String(16)),
             Column('reviewer', String(40)), Column('review_note', Text), Column('reviewed', Double))
schedules = Table('report_schedules', metadata, Column('id', Integer, primary_key=True),
                  Column('run_id', Integer, nullable=False), Column('recipient', String(100), nullable=False),
                  Column('cadence', String(16), nullable=False), Column('next_at', Double, nullable=False, index=True),
                  Column('active', Integer, nullable=False), Column('owner', String(40), nullable=False))
deliveries = Table('report_deliveries', metadata, Column('id', Integer, primary_key=True),
                   Column('schedule_id', Integer, nullable=False), Column('run_id', Integer, nullable=False),
                   Column('recipient', String(100), nullable=False), Column('scheduled', Double, nullable=False),
                   Column('delivered', Double, nullable=False), UniqueConstraint('schedule_id', 'scheduled'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))
