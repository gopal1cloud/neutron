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

import sys

from neutron.common import config as common_config
from neutron.openstack.common.rpc import proxy  # ICEHOUSE_BACKPORT
from neutron.plugins.cisco.common import cisco_constants as c_constants
from oslo.config import cfg

'''
How to run:
python rpc_tester.py --config-file /etc/neutron/neutron.conf
'''


class TestRPCNotifier(proxy.RpcProxy):
    BASE_RPC_API_VERSION = '1.0'

    def __init__(self, topic=c_constants.CFG_AGENT_L3_ROUTING):
        super(TestRPCNotifier, self).__init__(
            topic=topic, default_version=self.BASE_RPC_API_VERSION)

    def test_cast(self, method_name):
        context = {}
        self.cast(context,
                  self.make_msg(method_name),
                  topic='%s.%s' % (c_constants.CFG_AGENT_L3_ROUTING,
                                   "localhost.localdomain"))


def rpc_tester_main():

    conf = cfg.CONF
    common_config.parse(sys.argv[1:])
    conf(project='neutron')

    rpc_tester = TestRPCNotifier()
    rpc_tester.test_cast("resync_asrs")

if __name__ == "__main__":
    rpc_tester_main()
