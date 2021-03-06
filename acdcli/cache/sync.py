"""
Syncs Amazon Node API objects with sqlite database
"""

import logging
from sqlalchemy.exc import *
from sqlalchemy.sql.expression import func
from datetime import datetime, timedelta

try:
    import dateutil.parser as iso_date
except ImportError:
    # noinspection PyPep8Naming
    class iso_date(object):
        @staticmethod
        def parse(str_: str):
            return datetime.strptime(str_, '%Y-%m-%dT%H:%M:%S.%fZ')

from acdcli.cache import db

logger = logging.getLogger(__name__)

_CHECKPOINT_KEY = 'checkpoint'


def get_checkpoint() -> str:
    cp = db.session.query(db.Metadate).filter_by(key=_CHECKPOINT_KEY).first()
    return cp.value if cp else None


def set_checkpoint(cp: str):
    cp = db.Metadate(_CHECKPOINT_KEY, cp)
    db.session.merge(cp)
    db.session.commit()


def max_age() -> float:
    oldest = db.session.query(func.max(db.Node.updated)).scalar()
    if not oldest:
        return 0
    return (datetime.utcnow() - oldest) / timedelta(days=1)


def remove_purged(purged: list):
    for p_id in purged:
        n = db.session.query(db.Node).filter_by(id=p_id).first()
        if n:
            db.session.delete(n)
    db.session.commit()
    logger.info('Purged %i nodes.' % len(purged))


def insert_nodes(nodes: list):
    """Inserts mixed list of files and folders into cache."""
    files = []
    folders = []
    for node in nodes:
        if node['status'] == 'PENDING':
            continue
        kind = node['kind']
        if kind == 'FILE':
            files.append(node)
        elif kind == 'FOLDER':
            folders.append(node)
        elif kind != 'ASSET':
            logger.warning('Cannot insert unknown node type "%s".' % kind)
    insert_folders(folders)
    insert_files(files)


def insert_node(node: db.Node):
    """Inserts single file or folder into cache."""
    if not node:
        pass
    kind = node['kind']
    if kind == 'FILE':
        insert_files([node])
    elif kind == 'FOLDER':
        insert_folders([node])
    elif kind != 'ASSET':
        logger.warning('Cannot insert unknown node type "%s".' % kind)


def insert_folders(folders: list):
    """ Inserts list of folders into cache. Sets 'update' column to current date.
    :param folders: list of raw dict-type folders
    """

    ins = 0
    dup = 0
    upd = 0
    dtd = 0

    parent_pairs = []
    for folder in folders:
        logger.debug(folder)

        # root folder has no name key
        f_name = folder.get('name')
        f = db.Folder(folder['id'], f_name,
                      iso_date.parse(folder['createdDate']),
                      iso_date.parse(folder['modifiedDate']),
                      folder['status'])
        ef = db.session.query(db.Folder).filter_by(id=folder['id']).first()
        f.updated = datetime.utcnow()

        if not ef:
            db.session.add(f)
            ins += 1
        else:
            if f == ef:
                dup += 1
            else:
                upd += 1
            # this should keep the children intact
            db.session.merge(f)

        parent_pairs.append((f.id, folder['parents']))

    try:
        db.session.commit()
    except IntegrityError:
        logger.warning('Error inserting folders.')
        db.session.rollback()

    if ins > 0:
        logger.info(str(ins) + ' folder(s) inserted.')
    if dup > 0:
        logger.info(str(dup) + ' duplicate folders not inserted.')
    if upd > 0:
        logger.info(str(upd) + ' folder(s) updated.')
    if dtd > 0:
        logger.info(str(dtd) + ' folder(s) deleted.')

    conn = db.engine.connect()
    trans = conn.begin()
    for f in folders:
        conn.execute('DELETE FROM parentage WHERE child=?', f['id'])
    for rel in parent_pairs:
        for p in rel[1]:
            conn.execute('INSERT OR IGNORE INTO parentage VALUES (?, ?)', p, rel[0])
    trans.commit()


# file movement is detected by updated modifiedDate
def insert_files(files: list):
    ins = 0
    dup = 0
    upd = 0
    dtd = 0

    parent_pairs = []
    for file in files:
        props = {}
        try:
            props = file['contentProperties']
        except KeyError:  # empty files
            props['md5'] = 'd41d8cd98f00b204e9800998ecf8427e'
            props['size'] = 0

        f = db.File(file['id'], file['name'],
                    iso_date.parse(file['createdDate']),
                    iso_date.parse(file['modifiedDate']),
                    props['md5'], props['size'],
                    file['status'])
        ef = db.session.query(db.File).filter_by(id=file['id']).first()
        f.updated = datetime.utcnow()

        if not ef:
            db.session.add(f)
            ins += 1
        else:
            if f == ef:
                dup += 1
            else:
                upd += 1
            db.session.delete(ef)
            db.session.add(f)

        parent_pairs.append((f.id, file['parents']))

    try:
        db.session.commit()
    except ValueError:
        logger.error('Error inserting files.')
        db.session.rollback()

    if ins > 0:
        logger.info(str(ins) + ' file(s) inserted.')
    if upd > 0:
        logger.info(str(upd) + ' file(s) updated.')
    if dup > 0:
        logger.info(str(dup) + ' duplicate files not inserted.')
    if dtd > 0:
        logger.info(str(dtd) + ' file(s) deleted.')

    conn = db.engine.connect()
    trans = conn.begin()
    for f in files:
        conn.execute('DELETE FROM parentage WHERE child=?', f['id'])
    for rel in parent_pairs:
        for p in rel[1]:
            conn.execute('INSERT OR IGNORE INTO parentage VALUES (?, ?)', p, rel[0])
    trans.commit()