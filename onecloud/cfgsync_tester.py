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

import re
import xml.etree.ElementTree as ET

import ciscoconfparse
from ncclient import manager as nc_manager
import netaddr
import sys

from neutron.agent.common import config
from neutron.common import config as common_config
from neutron.common import constants
from neutron.common import rpc as n_rpc
from neutron import context as n_context
from neutron import manager
from neutron.openstack.common import loopingcall
from neutron.openstack.common.rpc import proxy  # ICEHOUSE_BACKPORT
from neutron.openstack.common import service
from neutron.plugins.cisco.common import cisco_constants as c_constants
from neutron import service as neutron_service
from oslo.config import cfg

from neutron.plugins.cisco.cfg_agent.device_drivers.csr1kv import (
    cisco_csr1kv_snippets as snippets)

'''
How to run:
python cfgsync_tester.py --config-file /etc/neutron/neutron.conf
'''

VRF_REGEX = "ip vrf nrouter-(\w{6,6})"
VRF_EXT_INTF_REGEX = "ip vrf forwarding .*"
VRF_INTF_REGEX = "ip vrf forwarding nrouter-(\w{6,6})"
INTF_REGEX = "interface Port-channel(\d+)\.(\d+)"
DOT1Q_REGEX = "encapsulation dot1Q (\d+)"
INTF_NAT_REGEX = "ip nat (inside|outside)"
HSRP_REGEX = "standby (\d+) .*"
SNAT_REGEX = "ip nat inside source static (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\
              (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}) vrf nrouter-(\w{6,6}) \
              redundancy neutron-hsrp-grp-(\d+)"
# IP_NAT_REGEX = "ip nat inside source static" \
# "(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})" \
# "(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}) vrf \w+ redundancy \w+"
NAT_OVERLOAD_REGEX = "ip nat inside source list neutron_acl_(\d+) \
interface Port-channel(\d+)\.(\d+) vrf nrouter-(\w+) overload"
ACL_REGEX = "ip access-list standard neutron_acl_(\d+)"
ACL_CHILD_REGEX = "\s*permit (\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}) \
(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})"


XML_FREEFORM_SNIPPET = "<config><cli-config-data>%s</cli-config-data></config>"
XML_CMD_TAG = "<cmd>%s</cmd>"


class CiscoRoutingPluginApi(proxy.RpcProxy):
    """RoutingServiceHelper(Agent) side of the  routing RPC API."""

    BASE_RPC_API_VERSION = '1.1'

    def __init__(self, topic, host):
        super(CiscoRoutingPluginApi, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)
        self.host = host

    def register(self, context):
        self.call(context,
                  self.make_msg('register_for_duty',
                                host=self.host),
                  topic=self.topic)

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
                         topic=self.topic)

    def create_rpc_dispatcher(self):
        return n_rpc.PluginRpcDispatcher([self])


class ConfigSyncTester(manager.Manager):

    def __init__(self):
        self.plugin_rpc = CiscoRoutingPluginApi("q-l3-plugin",
                                                "localhost.localdomain")
        self.context = n_context.get_admin_context_without_session()

        self.plugin_rpc.register(self.context)
        self.start_test_loop(self.test_get_routers, 60000)

    def register(self):
        self.plugin_rpc.register(self.context)

    def start_test_loop(self, loop_fn, loop_interval):
        self.loop = loopingcall.FixedIntervalLoopingCall(loop_fn)
        self.loop.start(interval=loop_interval)

    def testloop1(self):
        print("Hello")

    def get_all_routers(self):
        return self.plugin_rpc.get_routers(self.context)

    def process_routers_data(self, routers):
        router_id_dict = {}
        interface_segment_dict = {}
        segment_nat_dict = {}
        # TODO: could combine segment_nat_dict and interface_segment_dict
        #      into a single "segment_dict"

        for router in routers:

            # initialize router dict keyed by first 6 characters of router_id
            router_id = router['id'][0:6]
            router_id_dict[router_id] = router

            # initialize interface dict keyed by segment_id
            interfaces = []
            if '_interfaces' in router.keys():
                interfaces += router['_interfaces']

            if 'gw_port' in router.keys():
                interfaces += [router['gw_port']]

            if '_ha_gw_interfaces' in router.keys():
                interfaces += router['_ha_gw_interfaces']

            # Orgnize interfaces, indexed by segment_id
            for interface in interfaces:
                hosting_info = interface['hosting_info']
                segment_id = hosting_info['segmentation_id']
                if segment_id not in interface_segment_dict:
                    interface_segment_dict[segment_id] = []
                    segment_nat_dict[segment_id] = False
                interface_segment_dict[segment_id].append(interface)

            # Mark which segments have NAT enabled
            # i.e., the segment is present on at least 1 router with
            # both external and internal networks present
            if 'gw_port' in router.keys():
                gw_port = router['gw_port']
                gw_segment_id = gw_port['hosting_info']['segmentation_id']
                if '_interfaces' in router.keys():
                    interfaces = router['_interfaces']
                    for intf in interfaces:
                        if intf['device_owner'] == \
                            constants.DEVICE_OWNER_ROUTER_INTF:
                            intf_segment_id = intf['hosting_info']
                            ['segmentation_id']
                            segment_nat_dict[gw_segment_id] = True
                            segment_nat_dict[intf_segment_id] = True

        return router_id_dict, interface_segment_dict, segment_nat_dict

    def test_get_routers(self):
        routers = self.get_all_routers()
        router_id_dict, intf_segment_dict, segment_nat_dict = \
            self.process_routers_data(routers)

        print("*************************")

        for router_id, router in router_id_dict.iteritems():
            # print("ROUTER ID: %s   DATA: %s\n\n" % (router_id, router))
            print("ROUTER_ID: %s" % (router_id))

        print("\n")

        for segment_id, intf_list in intf_segment_dict.iteritems():
            print("SEGMENT_ID: %s" % (segment_id))
            for intf in intf_list:
                dev_owner = intf['device_owner']
                dev_id = intf['device_id'][0:6]
                ip_addr = intf['fixed_ips'][0]['ip_address']
                if 'phy_router_db' in intf.keys():
                    phy_router_name = intf['phy_router_db']['name']
                    print("    INTF: %s, %s, %s, %s" %
                          (ip_addr, dev_id, dev_owner, phy_router_name))
                else:
                    print("    INTF: %s, %s, %s" %
                          (ip_addr, dev_id, dev_owner))

        conn = self.connect()
        running_cfg = self.get_running_config(conn)
        parsed_cfg = ciscoconfparse.CiscoConfParse(running_cfg)

        self.clean_vrfs(conn, router_id_dict, parsed_cfg)
        self.clean_interfaces(conn, intf_segment_dict, segment_nat_dict,
                              parsed_cfg)
        self.clean_snat(conn, router_id_dict, intf_segment_dict,
                        segment_nat_dict, parsed_cfg)
        self.clean_nat_overload(conn, router_id_dict, intf_segment_dict,
                                segment_nat_dict, parsed_cfg)
        self.clean_acls(conn, intf_segment_dict, segment_nat_dict, parsed_cfg)

    def connect(self):
        asr_conn = nc_manager.connect(host="10.1.10.252",
                                      port=22,
                                      username="admin",
                                      password="!cisco123",
                                      allow_agent=False,
                                      look_for_keys=False,
                                      unknown_host_cb=lambda host,
                                      fingerprint: True,
                                      timeout=10)
        return asr_conn

    def get_running_config(self, conn):
        """Get the CSR's current running config.
        :return: Current IOS running config as multiline string
        """
        config = conn.get_config(source="running")
        if config:
            root = ET.fromstring(config._raw)
            running_config = root[0][0]
            rgx = re.compile("\r*\n+")
            ioscfg = rgx.split(running_config.text)
            return ioscfg

    def get_ostk_router_ids(self, router_id_dict):
        ostk_router_ids = []
        for router_id, router in router_id_dict.iteritems():
            ostk_router_ids.append(router_id)
        return ostk_router_ids

    def get_running_config_router_ids(self, parsed_cfg):
        rconf_ids = []

        for parsed_obj in parsed_cfg.find_objects(VRF_REGEX):
            print("VRF object: %s" % (parsed_obj))
            router_id = parsed_obj.re_match(VRF_REGEX)
            print("    First 6 digits of router ID: %s\n" % (router_id))
            rconf_ids.append(router_id)

        return rconf_ids

    def clean_vrfs(self, conn, router_id_dict, parsed_cfg):

        ostk_router_ids = self.get_ostk_router_ids(router_id_dict)
        rconf_ids = self.get_running_config_router_ids(parsed_cfg)

        source_set = set(ostk_router_ids)
        dest_set = set(rconf_ids)

        add_set = source_set.difference(dest_set)
        del_set = dest_set.difference(source_set)

        print("VRF DB set: %s" % (source_set))
        print("VRFs to delete: %s" % (del_set))
        print("VRFs to add: %s" % (add_set))

        for router_id in del_set:
            vrf_name = "nrouter-%s" % (router_id)
            confstr = snippets.REMOVE_VRF % vrf_name
            # rpc_obj = conn.edit_config(target='running', config=confstr)
            conn.edit_config(target='running', config=confstr)

        for router_id in add_set:
            vrf_name = "nrouter-%s" % (router_id)
            confstr = snippets.CREATE_VRF % vrf_name
            # rpc_obj = conn.edit_config(target='running', config=confstr)
            conn.edit_config(target='running', config=confstr)

    def get_single_cfg(self, cfg_line):
        if len(cfg_line) != 1:
            return None
        else:
            return cfg_line[0]

    def clean_snat(self, conn, router_id_dict, intf_segment_dict,
                   segment_nat_dict, parsed_cfg):
        delete_fip_list = []
        floating_ip_nats = parsed_cfg.find_objects(SNAT_REGEX)
        for snat_rule in floating_ip_nats:
            print("\nstatic nat rule: %s" % (snat_rule))
            match_obj = re.match(SNAT_REGEX, snat_rule.text)
            inner_ip, outer_ip, router_id, segment_id = match_obj.group(1, 2,
                                                                        3, 4)
            segment_id = int(segment_id)
            print("   in_ip: %s, out_ip: %s, router_id: %s, segment_id: %s" %
                  (inner_ip, outer_ip, router_id, segment_id))

            # Check that VRF exists in openstack DB info
            if router_id not in router_id_dict:
                print("router not found for rule, deleting")
                delete_fip_list.append(snat_rule.text)
                continue

            # Check that router has external network and segment_id matches
            router = router_id_dict[router_id]
            if "gw_port" not in router:
                print("router has no gw_port, snat is invalid, deleting")
                delete_fip_list.append(snat_rule.text)
                continue

            gw_port = router['gw_port']
            gw_segment_id = gw_port['hosting_info']['segmentation_id']
            if segment_id != gw_segment_id:
                print("snat segment_id does not match router's gw segment_id, \
                      deleting")
                delete_fip_list.append(snat_rule.text)
                continue

            # Check that in,out ip pair matches a floating_ip defined on router
            if '_floatingips' not in router:
                print("Router has no floating IPs defined, snat rule is \
                invalid, deleting")
                delete_fip_list.append(snat_rule.text)
                continue

            fip_match_found = False
            for floating_ip in router['_floatingips']:
                if inner_ip == floating_ip['fixed_ip_address'] and \
                   outer_ip == floating_ip['floating_ip_address']:
                    fip_match_found = True
                    break
            if fip_match_found is False:
                print("snat rule does not match defined floating IPs, \
                deleting")
                delete_fip_list.append(snat_rule.text)
                continue

        for fip_cfg in delete_fip_list:
            del_cmd = XML_CMD_TAG % ("no %s" % (fip_cfg))
            confstr = XML_FREEFORM_SNIPPET % (del_cmd)
            # rpc_obj = conn.edit_config(target='running', config=confstr)
            conn.edit_config(target='running', config=confstr)

    def clean_nat_overload(self, conn, router_id_dict, intf_segment_dict,
                           segment_nat_dict, parsed_cfg):
        delete_nat_list = []
        nat_overloads = parsed_cfg.find_objects(NAT_OVERLOAD_REGEX)
        for nat_rule in nat_overloads:
            print("\nnat overload rule: %s" % (nat_rule))
            match_obj = re.match(NAT_OVERLOAD_REGEX, nat_rule.text)
            segment_id, intf_num, intf_segment_id, router_id = \
                match_obj.group(1, 2, 3, 4)

            segment_id = int(segment_id)
            intf_num = int(intf_num)
            intf_segment_id = int(intf_segment_id)

            if segment_id != intf_segment_id:
                print("Interface segment and ACL segment mismatch, \
                deleting rule")
                delete_nat_list.append(nat_rule.text)
                continue

            # Check that VRF exists in openstack DB info
            if router_id not in router_id_dict:
                print("router not found for rule, deleting")
                delete_nat_list.append(nat_rule.text)
                continue

            # Check that router has external network
            router = router_id_dict[router_id]
            if "gw_port" not in router:
                print("router has no gw_port, nat overload is invalid, \
                deleting")
                delete_nat_list.append(nat_rule.text)
                continue

            # Check that router has internal network interface on segment_id
            intf_match_found = False
            for intf in router['_interfaces']:
                if intf['device_owner'] == constants.DEVICE_OWNER_ROUTER_INTF:
                    intf_segment_id = intf['hosting_info']['segmentation_id']
                    if intf_segment_id == segment_id:
                        intf_match_found = True
                        break
            if intf_match_found is False:
                print("router does not have this internal network assigned, \
                deleting rule")
                delete_nat_list.append(nat_rule.text)
                continue

        for nat_cfg in delete_nat_list:
            del_cmd = XML_CMD_TAG % ("no %s" % (nat_cfg))
            confstr = XML_FREEFORM_SNIPPET % (del_cmd)
            # rpc_obj = conn.edit_config(target='running', config=confstr)
            conn.edit_config(target='running', config=confstr)

    def check_acl_permit_rules_valid(self, segment_id, acl, intf_segment_dict):
        permit_rules = acl.re_search_children(ACL_CHILD_REGEX)
        for permit_rule in permit_rules:
            print("   permit rule: %s" % (permit_rule))
            match_obj = re.match(ACL_CHILD_REGEX, permit_rule.text)
            net_ip, hostmask = match_obj.group(1, 2)

            cfg_subnet = netaddr.IPNetwork("%s/%s" % (net_ip, hostmask))

            db_subnet = netaddr.IPNetwork("255.255.255.255/32")  # dummy value
            intf_list = intf_segment_dict[segment_id]
            for intf in intf_list:
                if intf['device_owner'] == constants.DEVICE_OWNER_ROUTER_INTF:
                    subnet_cidr = intf['subnet']['cidr']
                    db_subnet = netaddr.IPNetwork(subnet_cidr)
                    break

            print("   cfg_subnet: %s/%s, db_subnet: %s/%s" %
                  (cfg_subnet.network, cfg_subnet.prefixlen, db_subnet.network,
                   db_subnet.prefixlen))
            if cfg_subnet.network != db_subnet.network or \
               cfg_subnet.prefixlen != db_subnet.prefixlen:
                print("ACL subnet does not match subnet info in openstack DB, \
                deleting ACL")
                return False

        return True

    def clean_acls(self, conn, intf_segment_dict, segment_nat_dict,
                   parsed_cfg):
        delete_acl_list = []
        acls = parsed_cfg.find_objects(ACL_REGEX)
        for acl in acls:
            print("\nacl: %s" % (acl))
            match_obj = re.match(ACL_REGEX, acl.text)
            segment_id = match_obj.group(1)
            segment_id = int(segment_id)
            print("   segment_id: %s" % (segment_id))

            # Check that segment_id exists in openstack DB info
            if segment_id not in intf_segment_dict:
                print("Segment ID not found, deleting acl")
                delete_acl_list.append(acl.text)

            # Check that permit rules match subnets defined on openstack intfs
            if self.check_acl_permit_rules_valid(segment_id, acl,
                                                 intf_segment_dict) is False:
                delete_acl_list.append(acl.text)

        for acl_cfg in delete_acl_list:
            del_cmd = XML_CMD_TAG % ("no %s" % (acl_cfg))
            confstr = XML_FREEFORM_SNIPPET % (del_cmd)
            # rpc_obj = conn.edit_config(target='running', config=confstr)
            conn.edit_config(target='running', config=confstr)

    def clean_interfaces(self, conn, intf_segment_dict, segment_nat_dict,
                         parsed_cfg):

        runcfg_intfs = [obj for obj in parsed_cfg.find_objects("^interf")
                        if obj.re_search_children("description \
                        OPENSTACK_NEUTRON_INTF")]

        print("intf_segment_dict: %s" % (intf_segment_dict))
        pending_delete_list = []

        # TODO: split this big function into smaller functions
        for intf in runcfg_intfs:
            print("\nOpenstack interface: %s" % (intf))
            intf.intf_num = int(intf.re_match(INTF_REGEX, group=1))
            intf.segment_id = int(intf.re_match(INTF_REGEX, group=2))
            print("  num: %s  segment_id: %s" %
                  (intf.intf_num, intf.segment_id))

            # Delete any interfaces where config doesn't match DB
            # Correct config will be added after clearing invalid cfg

            # TODO: Check that interface name (e.g. Port-channel10) matches
            #       that specified in .ini file

            # Check that the interface segment_id exists in the current DB data
            if intf.segment_id not in intf_segment_dict:
                print("Invalid segment ID, delete interface")
                pending_delete_list.append(intf)
                continue

            # Check if dot1q config is correct
            dot1q_cfg = intf.re_search_children(DOT1Q_REGEX)
            dot1q_cfg = self.get_single_cfg(dot1q_cfg)

            if dot1q_cfg is None:
                print("Missing DOT1Q config, delete interface")
                pending_delete_list.append(intf)
                continue
            else:
                dot1q_num = int(dot1q_cfg.re_match(DOT1Q_REGEX, group=1))
                if dot1q_num != intf.segment_id:
                    print("DOT1Q mismatch, delete interface")
                    pending_delete_list.append(intf)
                    continue

            # Is this an "external network" segment_id?
            db_intf = intf_segment_dict[intf.segment_id][0]
            intf_type = db_intf["device_owner"]
            intf.is_external = (intf_type ==
                                constants.DEVICE_OWNER_ROUTER_HA_GW or
                                intf_type == constants.DEVICE_OWNER_ROUTER_GW)

            # Check VRF config
            if intf.is_external:
                vrf_cfg = intf.re_search_children(VRF_EXT_INTF_REGEX)
                vrf_cfg = self.get_single_cfg(vrf_cfg)
                print("VRF: %s" % (vrf_cfg))
                if vrf_cfg is not None:  # external network has no vrf
                    print("External network shouldn't have VRF, deleting intf")
                    pending_delete_list.append(intf)
                    continue
            else:
                vrf_cfg = intf.re_search_children(VRF_INTF_REGEX)
                vrf_cfg = self.get_single_cfg(vrf_cfg)
                print("VRF: %s" % (vrf_cfg))
                if not vrf_cfg:
                    print("Internal network missing valid VRF, deleting intf")
                    pending_delete_list.append(intf)
                    continue

                # check for VRF mismatch
                router_id = vrf_cfg.re_match(VRF_INTF_REGEX, group=1)
                if router_id != db_intf["device_id"][0:6]:
                    print("Internal network VRF mismatch, deleting intf")
                    pending_delete_list.append(intf)
                    continue

            # Checks beyond this point don't trigger intf delete

            # Fix NAT config
            intf_nat_type = intf.re_search_children(INTF_NAT_REGEX)
            intf_nat_type = self.get_single_cfg(intf_nat_type)

            if intf_nat_type is not None:
                intf_nat_type = intf_nat_type.re_match(INTF_NAT_REGEX, group=1)

            print("NAT Type: %s" % intf_nat_type)

            if segment_nat_dict[intf.segment_id] == True:
                if intf.is_external:
                    if intf_nat_type != "outside":
                        nat_cmd = XML_CMD_TAG % (intf.text)
                        nat_cmd += XML_CMD_TAG % ("ip nat outside")
                        confstr = XML_FREEFORM_SNIPPET % (nat_cmd)
                        print("NAT type mismatch, should be outside")
                        # rpc_obj = conn.edit_config(target='running',
                        #                            config=confstr)
                        conn.edit_config(target='running', config=confstr)
                else:
                    if intf_nat_type != "inside":
                        nat_cmd = XML_CMD_TAG % (intf.text)
                        nat_cmd += XML_CMD_TAG % ("ip nat inside")
                        confstr = XML_FREEFORM_SNIPPET % (nat_cmd)
                        print("NAT type mismatch, should be inside")
                        # rpc_obj = conn.edit_config(target='running',
                        #                            config=confstr)
                        conn.edit_config(target='running', config=confstr)
            else:
                if intf_nat_type is not None:
                    nat_cmd = XML_CMD_TAG % (intf.text)
                    nat_cmd += XML_CMD_TAG % ("no ip nat %s" % (intf_nat_type))
                    confstr = XML_FREEFORM_SNIPPET % (nat_cmd)
                    print("NAT type mismatch, should have no NAT")
                    # rpc_obj = conn.edit_config(target='running',
                    #                           config=confstr)
                    conn.edit_config(target='running', config=confstr)

            # Delete any hsrp config with wrong group number
            del_hsrp_cmd = XML_CMD_TAG % (intf.text)
            hsrp_cfg_list = intf.re_search_children(HSRP_REGEX)
            needs_hsrp_delete = False
            for hsrp_cfg in hsrp_cfg_list:
                hsrp_num = int(hsrp_cfg.re_match(HSRP_REGEX, group=1))
                if hsrp_num != intf.segment_id:
                    needs_hsrp_delete = True
                    del_hsrp_cmd += XML_CMD_TAG % ("no %s" % (hsrp_cfg.text))

            if needs_hsrp_delete:
                confstr = XML_FREEFORM_SNIPPET % (del_hsrp_cmd)
                print("Deleting bad HSRP config: %s" % (confstr))
                # rpc_obj = conn.edit_config(target='running', config=confstr)
                conn.edit_config(target='running', config=confstr)

        for intf in pending_delete_list:
            del_cmd = XML_CMD_TAG % ("no %s" % (intf.text))
            confstr = XML_FREEFORM_SNIPPET % (del_cmd)
            print("Deleting %s" % (intf.text))
            print(confstr)
            # rpc_obj = conn.edit_config(target='running', config=confstr)
            conn.edit_config(target='running', config=confstr)


class StandaloneService(neutron_service.Service):
    """Subclass that takes in pre-instantiated manager object \
       instead of class name
    """

    def __init__(self, host, binary, topic, manager, report_interval=None,
                 periodic_interval=None, periodic_fuzzy_delay=None,
                 *args, **kwargs):

        self.binary = binary
        self.report_interval = report_interval
        self.manager = manager
        self.periodic_interval = periodic_interval
        self.periodic_fuzzy_delay = periodic_fuzzy_delay
        self.saved_args, self.saved_kwargs = args, kwargs
        self.timers = []
        super(neutron_service.Service, self).__init__(host,
                                                      topic,
                                                      manager=self.manager)


def showrun_test_main():

    conf = cfg.CONF
    config.register_agent_state_opts_helper(conf)
    config.register_root_helper(conf)
    common_config.parse(sys.argv[1:])
    conf(project='neutron')
    config.setup_logging(conf)

    print("ciscoconfparse test")

    server = StandaloneService.create(
        binary='cfgsync_tester.py',
        topic=c_constants.CFG_AGENT,
        report_interval=10,
        manager=ConfigSyncTester()
    )

    service.launch(server).wait()


if __name__ == "__main__":
    showrun_test_main()
