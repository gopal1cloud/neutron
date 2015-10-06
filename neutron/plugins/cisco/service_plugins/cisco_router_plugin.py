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
#
# @author: Bob Melander, Cisco Systems, Inc.

from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.db import agents_db
# from neutron.db import common_db_mixin  # ICEHOUSE_BACKPORT
from neutron.db import db_base_plugin_v2
from neutron.db import l3_rpc_base
from neutron import manager
from neutron.plugins.cisco.db.l3 import device_handling_db
from neutron.plugins.cisco.db.l3 import l3_router_appliance_db
from neutron.plugins.cisco.l3.rpc import (l3_router_cfgagent_rpc_cb as
                                          l3_router_rpc)
from neutron.plugins.cisco.l3.rpc import devices_cfgagent_rpc_cb as devices_rpc
from neutron.plugins.common import constants

from neutron.openstack.common import rpc as o_rpc  # ICEHOUSE_BACKPORT
# from neutron.db import l3_db

# from neutron.openstack.common.notifier import api as notifier_api
# from neutron.api.v2 import attributes
# from neutron.db import models_v2
from neutron.openstack.common import log as logging
# from neutron.common import exceptions as n_exc
# from neutron.extensions import l3
# from neutron.common import constants as common_constants

# from neutron.plugins.cisco.cfg_agent.device_drivers.csr1kv import \
#  (asr1k_routing_driver as asr1k_driver)

LOG = logging.getLogger(__name__)


DEVICE_OWNER_ROUTER_HA_INTF = "network:router_ha_interface"
DEVICE_OWNER_ROUTER_HA_GW = "network:router_ha_gateway"
PHYSICAL_GLOBAL_ROUTER_ID = "PHYSICAL_GLOBAL_ROUTER_ID"


# class CiscoRouterPluginRpcCallbacks(n_rpc.RpcCallback, # ICEHOUSE_BACKPORT
class CiscoRouterPluginRpcCallbacks(l3_router_rpc.L3RouterCfgRpcCallbackMixin,
                                    l3_rpc_base.L3RpcCallbackMixin,
                                    devices_rpc.DeviceCfgRpcCallbackMixin):
    RPC_API_VERSION = '1.1'

    def __init__(self, l3plugin):
        super(CiscoRouterPluginRpcCallbacks, self).__init__()
        self._l3plugin = l3plugin

    @property
    def _core_plugin(self):
        return manager.NeutronManager.get_plugin()

    def create_rpc_dispatcher(self):
        return n_rpc.PluginRpcDispatcher([self])

# class CiscoRouterPlugin(common_db_mixin.CommonDbMixin, # ICEHOUSE_BACKPORT


class CiscoRouterPlugin(db_base_plugin_v2.CommonDbMixin,
                        agents_db.AgentDbMixin,
                        l3_router_appliance_db.L3RouterApplianceDBMixin,
                        device_handling_db.DeviceHandlingMixin):

    """Implementation of Cisco L3 Router Service Plugin for Neutron.

    This class implements a L3 service plugin that provides
    router and floatingip resources and manages associated
    request/response.
    All DB functionality is implemented in class
    l3_router_appliance_db.L3RouterApplianceDBMixin.
    """
    supported_extension_aliases = ["router", "extraroute"]

    def __init__(self):
        self.setup_rpc()
        # for backlogging of non-scheduled routers
        self._setup_backlog_handling()
        self._setup_device_handling()

    def setup_rpc(self):
        # RPC support
        self.topic = topics.L3PLUGIN
        # ICEHOUSE_BACKPORT
        # self.conn = n_rpc.create_connection(new=True)
        self.conn = o_rpc.create_connection(new=True)
        # ICEHOUSE_BACKPORT
        self.callbacks = CiscoRouterPluginRpcCallbacks(self)
        # ICEHOUSE_BACKPORT
        self.dispatcher = self.callbacks.create_rpc_dispatcher()
        self.endpoints = [CiscoRouterPluginRpcCallbacks(self)]
        # ICEHOUSE_BACKPORT
        # self.conn.create_consumer(self.topic, self.endpoints,
        #                           fanout=False)
        self.conn.create_consumer(self.topic, self.dispatcher,
                                  fanout=False)
        # Consume from all consumers in threads
        # self.conn.consume_in_threads()  # ICEHOUSE_BACKPORT
        self.conn.consume_in_thread()

    def get_plugin_type(self):
        return constants.L3_ROUTER_NAT

    def get_plugin_description(self):
        return ("Cisco Router Service Plugin for basic L3 forwarding"
                " between (L2) Neutron networks and access to external"
                " networks via a NAT gateway.")

    @property
    def _core_plugin(self):
        try:
            return self._plugin
        except AttributeError:
            self._plugin = manager.NeutronManager.get_plugin()
            return self._plugin
