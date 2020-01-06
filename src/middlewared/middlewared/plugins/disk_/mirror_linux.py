import glob
import os

from copy import deepcopy

from middlewared.service import CallError, Service
from middlewared.utils import filter_list, run

from .mirror_base import DiskMirrorBase


class DiskService(Service, DiskMirrorBase):

    async def create_mirror(self, name, options):
        extra = options['extra']
        cp = await run(
            'mdadm', '--build', os.path.join('/dev/md', name), f'--level={extra.get("level", 1)}',
            f'--raid-devices={len(options["paths"])}', *(options['paths']), encoding='utf8'
        )
        if cp.returncode:
            raise CallError('Failed to create mirror %s: %s', name, cp.stderr)

    async def destroy_mirror(self, name):
        # name here is path to the array
        mirror = await self.middleware.call('disk.get_mirrors', [['path', '=', name]], {'get': True})
        if mirror['encrypted_provider']:
            await self.middleware.call('disk.remove_encryption', mirror['encrypted_provider'])

        cp = await run('mdadm', '--stop', name, check=False, encoding='utf8')
        if cp.returncode:
            raise CallError('Failed to stop mirror %s: %s', mirror['name'], cp.stderr)

    def get_mirrors(self, filters, options):
        mirrors = []
        base_path = '/dev/md'
        for array in os.listdir(base_path) if os.path.exists(base_path) else []:
            mirror_data = deepcopy(self.mirror_base)
            mirror_data.update({
                'name': array,
                'path': os.path.join(base_path, array),
                'real_path': os.path.realpath(os.path.join(base_path, array)),
            })
            encrypted_path = glob.glob(f'/sys/block/dm-*/slaves/{mirror_data["real_path"].split("/")[-1]}')
            if encrypted_path:
                mirror_data['encrypted_provider'] = os.path.join('/dev', encrypted_path[0].split('/')[3])
            for provider in os.listdir(
                os.path.join('/sys/block', mirror_data['real_path'].split('/')[-1], 'slaves')
            ):
                provider_data = {'name': provider, 'id': provider}
                with open(os.path.join('/sys/class/block', provider, 'partition'), 'r') as f:
                    provider_data['disk'] = provider.rsplit(f.read().strip(), 1)[0].strip()
                mirror_data['providers'].append(provider_data)
            mirrors.append(mirror_data)
        return filter_list(mirrors, filters, options)
