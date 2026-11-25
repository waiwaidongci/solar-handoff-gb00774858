from sqlalchemy import JSON, Column, Double, Integer, MetaData, String, Table, Text, UniqueConstraint

metadata = MetaData()
users = Table('workspace_users', metadata, Column('username', String(40), primary_key=True),
              Column('role', String(20), nullable=False), Column('password', String(160), nullable=False))
datasets = Table('data_revisions', metadata, Column('id', Integer, primary_key=True),
                 Column('name', String(180), nullable=False), Column('checksum', String(64), nullable=False, unique=True),
                 Column('records', JSON, nullable=False), Column('owner', String(40), nullable=False),
                 Column('created', Double, nullable=False))
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
remediation_plans = Table('remediation_plans', metadata, Column('id', Integer, primary_key=True),
                          Column('dataset_id', Integer, nullable=False, index=True),
                          Column('shift_date', String(10), nullable=False, index=True),
                          Column('options', JSON, nullable=False), Column('result', JSON),
                          Column('revision', Integer, nullable=False), Column('state', String(16), nullable=False),
                          Column('owner', String(40), nullable=False), Column('created', Double, nullable=False),
                          Column('published', Double),
                          UniqueConstraint('dataset_id', 'shift_date'))
remediation_claims = Table('remediation_claims', metadata, Column('id', Integer, primary_key=True),
                           Column('plan_id', Integer, nullable=False, index=True), Column('device', String(60), nullable=False),
                           Column('zone', String(20), nullable=False), Column('crew', String(100), nullable=False),
                           Column('start_minute', Integer, nullable=False), Column('end_minute', Integer, nullable=False),
                           Column('owner', String(40), nullable=False), Column('created', Double, nullable=False),
                           UniqueConstraint('plan_id', 'device'))
remediation_dispatches = Table('remediation_dispatches', metadata, Column('id', Integer, primary_key=True),
                               Column('plan_id', Integer, nullable=False, index=True), Column('device', String(60), nullable=False),
                               Column('zone', String(20), nullable=False), Column('crew', String(100), nullable=False),
                               Column('start_minute', Integer, nullable=False), Column('end_minute', Integer, nullable=False),
                               Column('source', String(16), nullable=False), Column('revision', Integer, nullable=False),
                               Column('created', Double, nullable=False),
                               UniqueConstraint('plan_id', 'device'))
audit = Table('operation_audit', metadata, Column('id', Integer, primary_key=True),
              Column('at', Double, nullable=False), Column('actor', String(40), nullable=False),
              Column('action', String(30), nullable=False), Column('subject', Integer, nullable=False))
