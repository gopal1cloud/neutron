import collections
import eventlet
import netaddr

from oslo import messaging

from neutron.common import constants as l3_constants
from neutron.common import rpc as n_rpc
from neutron.common import topics
from neutron.common import utils as common_utils
from neutron import context as n_context
from neutron.openstack.common import excutils
from neutron.openstack.common import log as logging

from neutron.plugins.cisco.cfg_agent import cfg_exceptions
from neutron.plugins.cisco.cfg_agent.device_drivers import phy_driver_mgr as driver_mgr
from neutron.plugins.cisco.cfg_agent import device_status
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron.plugins.cisco.cfg_agent.service_helpers import routing_svc_helper
from neutron.plugins.cisco.cfg_agent.device_drivers.csr1kv import (asr1k_routing_driver as asr1kv_driver)


LOG = logging.getLogger(__name__)

N_ROUTER_PREFIX = 'nrouter-'

class RouterInfo(object):
    """Wrapper class around the (neutron) router dictionary.

    Information about the neutron router is exchanged as a python dictionary
    between plugin and config agent. RouterInfo is a wrapper around that dict,
    with attributes for common parameters. These attributes keep the state
    of the current router configuration, and are used for detecting router
    state changes when an updated router dict is received.

    This is a modified version of the RouterInfo class defined in the
    (reference) l3-agent implementation, for use with cisco config agent.
    """

    def __init__(self, router_id, router):
        self.router_id = router_id
        self.ex_gw_port = None
        self._snat_enabled = None
        self._snat_action = None
        self.internal_ports = []
        self.floating_ips = []
        self._router = None
        self.router = router
        self.routes = []
        self.ha_info = router.get('ha_info')
        self.ha_gw_ports = []

    @property
    def router(self):
        return self._router

    @property
    def id(self):
        return self.router_id

    @property
    def snat_enabled(self):
        return self._snat_enabled

    @router.setter
    def router(self, value):
        self._router = value
        if not self._router:
            return
        # enable_snat by default if it wasn't specified by plugin
        self._snat_enabled = self._router.get('enable_snat', True)

    def router_name(self):
        return N_ROUTER_PREFIX + self.router_id


class CiscoRoutingPluginApi(n_rpc.RpcProxy):
    """RoutingServiceHelper(Agent) side of the  routing RPC API."""

    BASE_RPC_API_VERSION = '1.1'

    def __init__(self, topic, host):
        super(CiscoRoutingPluginApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.host = host

class PhyCiscoRoutingPluginApi(n_rpc.RpcProxy):
    """RoutingServiceHelper(Agent) side of the  routing RPC API."""

    BASE_RPC_API_VERSION = '1.1'

    def __init__(self, topic, host):
        super(CiscoRoutingPluginApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.host = host

    def get_routers(self, context, router_ids=None, hd_ids=None):
        """Make a remote process call to retrieve the sync data for routers.

        :param context: session context
        :param router_ids: list of  routers to fetch
        :param hd_ids : hosting device ids, only routers assigned to these
                        hosting devices will be returned.
        """
        return self.call(context,
                         self.make_msg('sync_routers',
                                       host=self.host,
                                       router_ids=router_ids,
                                       hosting_device_ids=hd_ids),
                         topic=self.topic,
                         timeout=180)

    def agent_heartbeat(self, context):
        """Make a remote process call to check connectivity between
           agent and neutron-server

        :param context: session context
        """
        return self.call(context,
                         self.make_msg('agent_heartbeat',
                                       host=self.host),
                         topic=self.topic,
                         timeout=6)

    def create_rpc_dispatcher(self):
        return n_rpc.PluginRpcDispatcher([self])



class PhyRouterContext(routing_svc_helper.RoutingServiceHelper):

    def __init__(self, asr_ent, plugin_rpc, context, dev_status):
        self.router_info = {}
        self.updated_routers = set()
        self.removed_routers = set()
        self.sync_devices = set()
        self.fullsync = True
        self.plugin_rpc = plugin_rpc
        self.context = context
        self._dev_status = dev_status
        self._drivermgr = driver_mgr.PhysicalDeviceDriverManager(asr_ent)
        self._drivermgr.set_driver(None)
        driver = self._drivermgr.get_driver(None)
        driver.set_err_listener_context(self)

    def connection_err_callback(self, ex):
        LOG.exception("NetConf connection exception: %s" % (ex))
        self.fullsync = True

    def delete_invalid_cfg(self, router_db_info):
        if router_db_info is None:
            router_db_info = self._fetch_router_info(all_routers=True)
        driver = self._drivermgr.get_driver(None)
        existing_cfg_dict = driver.delete_invalid_cfg(router_db_info)
        return existing_cfg_dict

    def prepare_fullsync(self, existing_cfg_dict):
        driver = self._drivermgr.get_driver(None)
        driver.prepare_fullsync(existing_cfg_dict)

    def clear_fullsync(self):
        driver = self._drivermgr.get_driver(None)
        driver.clear_fullsync()

    def process_service(self, device_ids=None, removed_devices_info=None):
        try:
            LOG.info("Sending heartbeat to ASR")
            self._drivermgr.get_driver(None).send_empty_cfg()
            LOG.debug("Routing service processing started")
            resources = {}
            routers = []
            removed_routers = []
            all_routers_flag = False
            if self.fullsync:
                LOG.debug("FullSync flag is on. Starting fullsync")
                # Setting all_routers_flag and clear the global full_sync flag
                all_routers_flag = True
                self.fullsync = False
                self.updated_routers.clear()
                self.removed_routers.clear()
                self.sync_devices.clear()
                routers = self._fetch_router_info(all_routers=True)
                existing_cfg_dict = self.delete_invalid_cfg(routers)
                self.prepare_fullsync(existing_cfg_dict)
                self.router_info = {}
            else:
                if self.updated_routers:
                    router_ids = list(self.updated_routers)
                    LOG.debug("Updated routers:%s", router_ids)
                    self.updated_routers.clear()
                    routers = self._fetch_router_info(router_ids=router_ids)
                if self.removed_routers:
                    removed_routers_ids = list(self.removed_routers)
                    LOG.debug("Removed routers:%s", removed_routers_ids)
                    for r in removed_routers_ids:
                        if r in self.router_info:
                            removed_routers.append(self.router_info[r].router)

            # Sort on hosting device
            if routers:
                resources['routers'] = routers
            if removed_routers:
                resources['removed_routers'] = removed_routers

            # Dispatch process_services() for each hosting device
            #pool = eventlet.GreenPool()
            #pool.spawn_n(self._process_routers, routers, removed_routers,
            #             0, all_routers=all_routers_flag)
            #pool.waitall()
            self._process_routers(routers, removed_routers, 0, all_routers=all_routers_flag)
            self.clear_fullsync()
        except Exception:
            LOG.exception(_("Failed processing routers"))
            self.fullsync = True


    def _adjust_router_list(self, routers):
        for r in routers:
            if r['id'] == "PHYSICAL_GLOBAL_ROUTER_ID":
                routers.remove(r)
                routers.append(r)
                return

    def _process_routers(self, routers, removed_routers,
                         device_id=None, all_routers=False):
        """Process the set of routers.

        Iterating on the set of routers received and comparing it with the
        set of routers already in the routing service helper, new routers
        which are added are identified. Before processing check the
        reachability (via ping) of hosting device where the router is hosted.
        If device is not reachable it is backlogged.

        For routers which are only updated, call `_process_router()` on them.

        When all_routers is set to True (because of a full sync),
        this will result in the detection and deletion of routers which
        have been removed.

        Whether the router can only be assigned to a particular hosting device
        is decided and enforced by the plugin. No checks are done here.

        :param routers: The set of routers to be processed
        :param removed_routers: the set of routers which where removed
        :param device_id: Id of the hosting device
        :param all_routers: Flag for specifying a partial list of routers
        :return: None
        """
        try:
            if all_routers:
                prev_router_ids = set(self.router_info)
            else:
                prev_router_ids = set(self.router_info) & set(
                    [router['id'] for router in routers])
            cur_router_ids = set()

            deleted_id_list = []

            for r in routers:
                if not r['admin_state_up']:
                        continue
                cur_router_ids.add(r['id'])

            # identify and remove routers that no longer exist
            for router_id in prev_router_ids - cur_router_ids:
                self._router_removed(router_id)
                deleted_id_list.append(router_id)

            if removed_routers:
                for router in removed_routers:
                    self._router_removed(router['id'])
                    deleted_id_list.append(router['id'])

            self._adjust_router_list(routers)
            for r in routers:
                if r['id'] in deleted_id_list:
                    continue

                try:
                    if not r['admin_state_up']:
                        continue
                    cur_router_ids.add(r['id'])

                    if r['id'] not in self.router_info:
                        self._router_added(r['id'], r)
                    ri = self.router_info[r['id']]
                    ri.router = r
                    self._process_router(ri)
                except KeyError as e:
                    LOG.exception(_("Key Error, missing key: %s"), e)
                    self.updated_routers.update([r['id']]) # make sure the ID is in a list (for set.update)
                    self.fullsync = True
                    continue
                except cfg_exceptions.DriverException as e:
                    LOG.exception(_("Driver Exception on router:%(id)s. "
                                    "Error is %(e)s"), {'id': r['id'], 'e': e})
                    self.updated_routers.update([r['id']])
                    self.fullsync = True # TODO: Do fullsync on error to be safe for now, can optimize later
                    continue

            # identify and remove routers that no longer exist
            #for router_id in prev_router_ids - cur_router_ids:
            #    self._router_removed(router_id)
            #if removed_routers:
            #    for router in removed_routers:
            #        self._router_removed(router['id'])
            
        except Exception:
            LOG.exception(_("Exception in processing routers on device:%s"),
                          device_id)
            self.sync_devices.add(device_id)

    def _get_port_set_diffs(self, existing_list, current_list):
        existing_port_ids = set([p['id'] for p in existing_list])
        current_port_ids = set([p['id'] for p in current_list
                                if p['admin_state_up']])
        new_ports = [p for p in current_list
                     if
                     p['id'] in (current_port_ids - existing_port_ids)]
        old_ports = [p for p in existing_list
                     if p['id'] not in current_port_ids]

        return old_ports, new_ports

    def _process_router(self, ri):
        """Process a router, apply latest configuration and update router_info.

        Get the router dict from  RouterInfo and proceed to detect changes
        from the last known state. When new ports or deleted ports are
        detected, `internal_network_added()` or `internal_networks_removed()`
        are called accordingly. Similarly changes in ex_gw_port causes
         `external_gateway_added()` or `external_gateway_removed()` calls.
        Next, floating_ips and routes are processed. Also, latest state is
        stored in ri.internal_ports and ri.ex_gw_port for future comparisons.

        :param ri : RouterInfo object of the router being processed.
        :return:None
        :raises: neutron.plugins.cisco.cfg_agent.cfg_exceptions.DriverException
        if the configuration operation fails.
        """
        try:
            ex_gw_port = ri.router.get('gw_port')
            ri.ha_info = ri.router.get('ha_info', None)
            internal_ports = ri.router.get(l3_constants.INTERFACE_KEY, [])
            gw_ports = ri.router.get(l3_constants.HA_GW_KEY, [])

            old_ports, new_ports = self._get_port_set_diffs(ri.internal_ports, internal_ports)
            old_gw_ports, new_gw_ports = self._get_port_set_diffs(ri.ha_gw_ports, gw_ports)

            for p in new_ports:
                self._set_subnet_info(p)
                self._internal_network_added(ri, p, ex_gw_port)
                ri.internal_ports.append(p)

            for p in old_ports:
                self._internal_network_removed(ri, p, ri.ex_gw_port)
                ri.internal_ports.remove(p)

            for p in new_gw_ports:
                self._set_subnet_info(p)
                self._external_gateway_added(ri, p)
                ri.ha_gw_ports.append(p)

            for p in old_gw_ports:
                self._external_gateway_removed(ri, p)
                ri.ha_gw_ports.remove(p)

            # if ex_gw_port and not ri.ex_gw_port:
            #     self._set_subnet_info(ex_gw_port)
            #     self._external_gateway_added(ri, ex_gw_port)
            # elif not ex_gw_port and ri.ex_gw_port:
            #     self._external_gateway_removed(ri, ri.ex_gw_port)

            if ex_gw_port:
                self._process_router_floating_ips(ri, ex_gw_port)

            ri.ex_gw_port = ex_gw_port
            self._routes_updated(ri)
        except cfg_exceptions.DriverException as e:
            with excutils.save_and_reraise_exception():
                self.updated_routers.update([ri.router_id])
                LOG.error(e)



class RoutingServiceHelperWithPhyContext(routing_svc_helper.RoutingServiceHelper):

    def __init__(self, host, conf, cfg_agent):
        self.conf = conf
        self.cfg_agent = cfg_agent
        self.context = n_context.get_admin_context_without_session()
        self.plugin_rpc = PhyCiscoRoutingPluginApi(topics.L3PLUGIN, host)
        self._dev_status = device_status.DeviceStatus()        
        self.topic = '%s.%s' % (c_constants.CFG_AGENT_L3_ROUTING, host)
        self._setup_rpc()
        self._asr_config = asr1kv_driver.ASR1kConfigInfo()

        self._asr_contexts = {}
        for asr in self._asr_config.get_asr_list():
            self._asr_contexts[asr['name']] = PhyRouterContext(asr, 
                                                               self.plugin_rpc, 
                                                               self.context,
                                                               self._dev_status)
        

    ### Notifications from Plugin ####

    def router_deleted(self, context, routers):
        """Deal with router deletion RPC message."""
        LOG.debug('Got router deleted notification for %s', routers)
        for asr_name, asr_ctx in self._asr_contexts.iteritems():
            asr_ctx.removed_routers.update(routers)

    def routers_updated(self, context, routers):
        """Deal with routers modification and creation RPC message."""
        LOG.debug('Got routers updated notification :%s', routers)
        if routers:
            # This is needed for backward compatibility
            if isinstance(routers[0], dict):
                routers = [router['id'] for router in routers]
            for asr_name, asr_ctx in self._asr_contexts.iteritems():
                asr_ctx.updated_routers.update(routers)

    def router_removed_from_agent(self, context, payload):
        LOG.debug('Got router removed from agent :%r', payload)
        for asr_name, asr_ctx in self._asr_contexts.iteritems():
            asr_ctx.removed_routers.add(payload['router_id'])

    def router_added_to_agent(self, context, payload):
        LOG.debug('Got router added to agent :%r', payload)
        self.routers_updated(context, payload)

    ### General Notifications  ####
    def resync_asrs(self, context):
        for asr_name, asr_ctx in self._asr_contexts.iteritems():
            asr_ctx.fullsync = True

    # Routing service helper public methods
    def process_service(self, device_ids=None, removed_devices_info=None):
        
        try:
            self.plugin_rpc.agent_heartbeat(self.context)
        except messaging.MessagingTimeout:
            LOG.exception("Server heartbeat timeout")
            self.resync_asrs(self.context)
            return # don't try to configure ASRs, can't get latest DB info

        pool = eventlet.GreenPool()
        for asr_name, asr_ctx in self._asr_contexts.iteritems():
            pool.spawn_n(asr_ctx.process_service, device_ids, removed_devices_info)
        pool.waitall()

    def collect_state(self, configurations):
        if len(self._asr_contexts) < 1:
            return configurations
        
        asr_ctx = self._asr_contexts.values()[0]
        return asr_ctx.collect_state(configurations)
    
