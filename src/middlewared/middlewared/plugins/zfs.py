from bsd import geom

from middlewared.schema import Dict, List, Str, Bool, Int, accepts
from middlewared.service import CallError, CRUDService, Service, job, private

import errno
import libzfs
import threading
import time


def find_vdev(pool, vname):
    """
    Find a vdev in the given `pool` using `vname` looking for
    guid or path

    Returns:
        libzfs.ZFSVdev object
    """
    children = list(pool.root_vdev.children)
    while children:
        child = children.pop()

        if child.guid == vname:
            return child

        if child.type == 'disk':
            path = child.path.replace('/dev/', '')
            if path == vname:
                return child

        children += list(child.children)


class ZFSPoolService(Service):

    class Config:
        namespace = 'zfs.pool'
        private = True

    @accepts(Str('pool'))
    async def get_disks(self, name):
        zfs = libzfs.ZFS()
        try:
            zpool = zfs.get(name)
        except libzfs.ZFSException as e:
            raise CallError(str(e), errno.ENOENT)

        await self.middleware.threaded(geom.scan)
        labelclass = geom.class_by_name('LABEL')
        for absdev in zpool.disks:
            dev = absdev.replace('/dev/', '').replace('.eli', '')
            find = labelclass.xml.findall(f".//provider[name='{dev}']/../consumer/provider")
            name = None
            if find:
                name = geom.provider_by_id(find[0].get('ref')).geom.name
            else:
                g = geom.geom_by_name('DEV', dev)
                if g:
                    name = g.consumer.provider.geom.name

            if name and geom.geom_by_name('DISK', name):
                yield name
            else:
                self.logger.debug(f'Could not find disk for {dev}')

    @accepts(
        Str('name'),
        List('new'),
        List('existing', items=[
            Dict(
                'attachvdev',
                Str('target'),
                Str('type', enum=['DISK']),
                Str('path'),
            ),
        ]),
    )
    @job()
    def extend(self, job, name, new=None, existing=None):
        """
        Extend a zfs pool `name` with `new` vdevs or attach to `existing` vdevs.
        """

        if new is None and existing is None:
            raise CallError('New or existing vdevs must be provided', errno.EINVAL)

        if new:
            raise CallError('Adding new vdev is not implemented yet')

        try:
            zfs = libzfs.ZFS()
            pool = zfs.get(name)

            # Make sure we can find all target vdev
            for i in (existing or []):
                target = find_vdev(pool, i['target'])
                if target is None:
                    raise CallError(f'Failed to find vdev for {target}', errno.EINVAL)
                i['target'] = target

            for i in (existing or []):
                newvdev = libzfs.ZFSVdev(zfs, i['type'].lower())
                newvdev.path = i['path']
                i['target'].attach(newvdev)

        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

    @accepts(Str('pool'), Str('label'))
    def detach(self, name, label):
        """
        Detach device `label` from the pool `pool`.
        """
        try:
            zfs = libzfs.ZFS()
            pool = zfs.get(name)
            target = find_vdev(pool, label)
            if target is None:
                raise CallError(f'Failed to find vdev for {label}', errno.EINVAL)
            target.detach()
        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

    @accepts(Str('pool'), Str('label'), Str('dev'))
    def replace(self, name, label, dev):
        """
        Replace device `label` with `dev` in pool `name`.
        """
        try:
            zfs = libzfs.ZFS()
            pool = zfs.get(name)
            target = find_vdev(pool, label)
            if target is None:
                raise CallError(f'Failed to find vdev for {label}', errno.EINVAL)

            newvdev = libzfs.ZFSVdev(zfs, 'disk')
            newvdev.path = f'/dev/{dev}'
            target.replace(newvdev)
        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

    @accepts(Str('name'))
    @job(lock=lambda i: i[0])
    def scrub(self, job, name):
        """
        Start a scrub on pool `name`.
        """
        try:
            zfs = libzfs.ZFS()
            pool = zfs.get(name)
            pool.start_scrub()
        except libzfs.ZFSException as e:
            raise CallError(str(e), e.code)

        def watch():
            while True:
                scrub = pool.scrub
                if scrub.function != libzfs.ScanFunction.SCRUB:
                    break

                if scrub.state == libzfs.ScanState.FINISHED:
                    job.set_progress(100, 'Scrub finished')
                    break

                if scrub.state == libzfs.ScanState.CANCELED:
                    break

                if scrub.state == libzfs.ScanState.SCANNING:
                    job.set_progress(scrub.percentage, 'Scrubbing')
                time.sleep(1)

        t = threading.Thread(target=watch, daemon=True)
        t.start()
        t.join()


class ZFSSnapshot(CRUDService):

    class Config:
        namespace = 'zfs.snapshot'

    @accepts(Dict(
        'snapshot_create',
        Str('dataset'),
        Str('name'),
        Bool('recursive'),
        Int('vmsnaps_count')
    ))
    async def do_create(self, data):
        """
        Take a snapshot from a given dataset.

        Returns:
            bool: True if succeed otherwise False.
        """
        zfs = libzfs.ZFS()

        dataset = data.get('dataset', '')
        name = data.get('name', '')
        recursive = data.get('recursive', False)
        vmsnaps_count = data.get('vmsnaps_count', 0)

        if not dataset or not name:
            return False

        try:
            ds = zfs.get_dataset(dataset)
        except libzfs.ZFSException as err:
            self.logger.error("{0}".format(err))
            return False

        try:
            if recursive:
                ds.snapshots('{0}@{1}'.format(dataset, name, recursive=True))
            else:
                ds.snapshot('{0}@{1}'.format(dataset, name))

            if vmsnaps_count > 0:
                ds.properties['freenas:vmsynced'] = libzfs.ZFSUserProperty('Y')

            self.logger.info("Snapshot taken: {0}@{1}".format(dataset, name))
            return True
        except libzfs.ZFSException as err:
                self.logger.error("{0}".format(err))
                return False

    @accepts(Dict(
        'snapshot_remove',
        Str('dataset'),
        Str('name')
    ))
    async def remove(self, data):
        """
        Remove a snapshot from a given dataset.

        Returns:
            bool: True if succeed otherwise False.
        """
        zfs = libzfs.ZFS()

        dataset = data.get('dataset', '')
        snapshot_name = data.get('name', '')

        if not dataset or not snapshot_name:
            return False

        try:
            ds = zfs.get_dataset(dataset)
        except libzfs.ZFSException as err:
            self.logger.error("{0}".format(err))
            return False

        __snap_name = dataset + '@' + snapshot_name
        try:
            for snap in list(ds.snapshots):
                if snap.name == __snap_name:
                    ds.destroy_snapshot(snapshot_name)
                    self.logger.info("Destroyed snapshot: {0}".format(__snap_name))
                    return True
            self.logger.error("There is no snapshot {0} on dataset {1}".format(snapshot_name, dataset))
            return False
        except libzfs.ZFSException as err:
            self.logger.error("{0}".format(err))
            return False

    @accepts(Dict(
        'snapshot_clone',
        Str('snapshot'),
        Str('dataset_dst'),
    ))
    async def clone(self, data):
        """
        Clone a given snapshot to a new dataset.

        Returns:
            bool: True if succeed otherwise False.
        """
        zfs = libzfs.ZFS()

        snapshot = data.get('snapshot', '')
        dataset_dst = data.get('dataset_dst', '')

        if not snapshot or not dataset_dst:
            return False

        try:
            snp = zfs.get_snapshot(snapshot)
        except libzfs.ZFSException as err:
            self.logger.error("{0}".format(err))
            return False

        try:
            snp.clone(dataset_dst)
            self.logger.info("Cloned snapshot {0} to dataset {1}".format(snapshot, dataset_dst))
            return True
        except libzfs.ZFSException as err:
            self.logger.error("{0}".format(err))
            return False

    @private
    def query(self):
        """
            XXX: Just set it as private and avoid show a query method
                 on the API documentation that was not implemented yet.
        """
        pass