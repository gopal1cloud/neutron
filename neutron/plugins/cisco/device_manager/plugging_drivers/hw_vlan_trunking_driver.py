# Copyright 2014 Cisco Systems, Inc.  All rights reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import eventlet

from oslo_log import log as logging
from oslo_utils import excutils
from sqlalchemy.sql import expression as expr

from neutron.api.v2 import attributes
from neutron.common import constants as l3_constants
from neutron.common import exceptions as n_exc
from neutron.db import models_v2
from neutron.extensions import providernet as pr_net
from neutron.i18n import _LE, _LI, _LW
from neutron.plugins.cisco.device_manager import config
from neutron.plugins.cisco.device_manager.plugging_drivers import (
    n1kv_trunking_driver)

LOG = logging.getLogger(__name__)


DELETION_ATTEMPTS = 5
SECONDS_BETWEEN_DELETION_ATTEMPTS = 3


class HwVLANTrunkingPlugDriver(n1kv_trunking_driver.N1kvTrunkingPlugDriver):
    """Driver class for Cisco hardware-based devices.

    The driver works with VLAN segmented Neutron networks.
    """
    # once initialized _device_network_interface_map is dictionary
    _device_network_interface_map = None

    def create_hosting_device_resources(self, context, complementary_id,
                                        tenant_id, mgmt_context, max_hosted):
        mgmt_port = None
        if mgmt_context and mgmt_context.get('mgmt_nw_id') and tenant_id:
            # Create port for mgmt interface
            p_spec = {'port': {
                'tenant_id': tenant_id,
                'admin_state_up': True,
                'name': 'mgmt',
                'network_id': mgmt_context['mgmt_nw_id'],
                'mac_address': attributes.ATTR_NOT_SPECIFIED,
                'fixed_ips': self._mgmt_subnet_spec(context, mgmt_context),
                'device_id': "",
                # Use device_owner attribute to ensure we can query for these
                # ports even before Nova has set device_id attribute.
                'device_owner': complementary_id}}
            try:
                # perform this operation in this try-except to allow us to
                # use this driver also with other plugins than n1kv monolitic
                p_spec['n1kv:profile_id'] = self.mgmt_port_profile_id()
            except AttributeError:
                # this error means that we're not using the n1kv plugin so
                # the profile_id attribute should not be included
                pass
            try:
                mgmt_port = self._core_plugin.create_port(context,
                                                          p_spec)
            except n_exc.NeutronException as e:
                LOG.error(_LE('Error %s when creating service VM resources. '
                              'Cleaning up.'), e)
                resources = {}
                self.delete_hosting_device_resources(
                    context, tenant_id, mgmt_port, **resources)
                mgmt_port = None
        return {'mgmt_port': mgmt_port}

    def get_hosting_device_resources(self, context, id, complementary_id,
                                     tenant_id, mgmt_nw_id):
        ports, nets, subnets = [], [], []
        mgmt_port = None
        # Ports for hosting device may not yet have 'device_id' set to
        # Nova assigned uuid of VM instance. However, those ports will still
        # have 'device_owner' attribute set to complementary_id. Hence, we
        # use both attributes in the query to ensure we find all ports.
        query = context.session.query(models_v2.Port)
        query = query.filter(expr.or_(
            models_v2.Port.device_id == id,
            models_v2.Port.device_owner == complementary_id))
        for port in query:
            if port['network_id'] != mgmt_nw_id:
                ports.append(port)
                nets.append({'id': port['network_id']})
                subnets.append({'id': port['fixed_ips'][0]['subnet_id']})
            else:
                mgmt_port = port
        return {'mgmt_port': mgmt_port,
                'ports': ports, 'networks': nets, 'subnets': subnets}

    def delete_hosting_device_resources(self, context, tenant_id, mgmt_port,
                                        **kwargs):
        attempts = 1
        while mgmt_port is not None:
            if attempts == DELETION_ATTEMPTS:
                LOG.warning(_LW('Aborting resource deletion after %d '
                                'unsuccessful attempts'), DELETION_ATTEMPTS)
                return
            else:
                if attempts > 1:
                    eventlet.sleep(SECONDS_BETWEEN_DELETION_ATTEMPTS)
                LOG.info(_LI('Resource deletion attempt %d starting'),
                         attempts)
            # Remove anything created.
            if mgmt_port is not None:
                ml = {mgmt_port['id']}
                self._delete_resources(context, "management port",
                                       self._core_plugin.delete_port,
                                       n_exc.PortNotFound, ml)
                if not ml:
                    mgmt_port = None
            attempts += 1
        LOG.info(_LI('Resource deletion succeeded'))

    def _delete_resources(self, context, name, deleter, exception_type,
                          resource_ids):
        for item_id in resource_ids.copy():
            try:
                deleter(context, item_id)
                resource_ids.remove(item_id)
            except exception_type:
                resource_ids.remove(item_id)
            except n_exc.NeutronException as e:
                LOG.error(_LE('Failed to delete %(resource_name) %(net_id)s '
                              'for service vm due to %(err)s'),
                          {'resource_name': name, 'net_id': item_id, 'err': e})

    def setup_logical_port_connectivity(self, context, port_db,
                                        hosting_device_id):
        pass

    def teardown_logical_port_connectivity(self, context, port_db,
                                           hosting_device_id):
        pass

    def extend_hosting_port_info(self, context, port_db, hosting_device,
                                 hosting_info):
        hosting_info['segmentation_id'] = port_db.hosting_info.segmentation_id
        is_external = port_db.get('router_port', {}).get(
            'port_type') == l3_constants.DEVICE_OWNER_ROUTER_GW
        hosting_info['physical_interface'] = self._get_interface_info(
            hosting_device['id'], port_db.network_id, is_external)

    def allocate_hosting_port(self, context, router_id, port_db, network_type,
                              hosting_device_id):
        # For VLAN core plugin provides VLAN tag
        tags = self._core_plugin.get_networks(
            context, {'id': [port_db['network_id']]}, [pr_net.SEGMENTATION_ID])
        allocated_vlan = (None if tags == []
                          else tags[0].get(pr_net.SEGMENTATION_ID))
        if allocated_vlan is None:
            # Database must have been messed up if this happens ...
            LOG.debug('hw_vlan_trunking_driver: Could not allocate VLAN')
            return
        return {'allocated_port_id': port_db.id,
                'allocated_vlan': allocated_vlan}

    @classmethod
    def _get_interface_info(cls, device_id, network_id, external=False):
        if cls._device_network_interface_map is None:
            cls._get_network_interface_map_from_config()
        try:
            dev_info = cls._device_network_interface_map[device_id]
            if external:
                return dev_info['external'].get(network_id,
                                                dev_info['external']['*'])
            else:
                return dev_info['internal'].get(network_id,
                                                dev_info['internal']['*'])
        except (TypeError, KeyError):
            LOG.error(_LE('Failed to lookup interface on device %(dev)s'
                          'for network %(net)s'), {'dev': device_id,
                                                   'net': network_id})
            return

    @classmethod
    def _get_network_interface_map_from_config(cls):
        dni_dict = config.get_specific_config(
            'HwVLANTrunkingPlugDriver'.lower())
        temp = {}
        for hd_uuid, kv_dict in dni_dict.items():
            # ensure hd_uuid is properly formatted
            hd_uuid = config.uuidify(hd_uuid)
            if hd_uuid not in temp:
                temp[hd_uuid] = {'internal': {}, 'external': {}}
            for k, v in kv_dict.items():
                try:
                    entry = k[:k.index('_')]
                    net_spec, interface = v.split(':')
                    for net_id in net_spec.split(','):
                        temp[hd_uuid][entry][net_id] = interface
                except (ValueError, KeyError):
                    with excutils.save_and_reraise_exception() as ctx:
                        ctx.reraise = False
                        LOG.error(_LE('Invalid network to interface mapping '
                                      '%(key)s, %(value)s in configuration '
                                      'file for device = %(dev)s'),
                                  {'key': k, 'value': v, 'dev': hd_uuid})
        cls._device_network_interface_map = temp
