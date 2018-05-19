# Copyright (C) 2018 XuanYao Zhang @ University of Illinois at Urbana-Champaign
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from ryu.base import app_manager
from ryu.controller import ofp_event
from ryu.controller.handler import HANDSHAKE_DISPATCHER ,CONFIG_DISPATCHER, MAIN_DISPATCHER
from ryu.controller.handler import set_ev_cls
from ryu.ofproto import ofproto_v1_4

from ryu.lib import hub, ip
from ryu.lib.packet import packet
from ryu.lib.packet import ethernet, ipv4, in_proto
from ryu.lib.packet import ether_types

# Custom lib
from lib.msg_decoder import ethertype_bits_to_name, ofpet_no_to_text
from lib.igmplib import IgmpQuerier
from lib.io import new_fifo_window


class Gateways(app_manager.RyuApp):
	OFP_VERSIONS = [ofproto_v1_4.OFP_VERSION]

	def __init__(self, *args, **kwargs):
		super(Gateways, self).__init__(*args, **kwargs)

		#tmp_fifo = '/tmp/xterm_ryu_monitor_1'
		#self.monitor1 = new_fifo_window(tmp_fifo)
		#self.monitor1.write("hello @ " + tmp_fifo)

		self.initialised_switches = set()

		self.mac_to_port = {}
		self.ip_to_port = {}
		self.port_stats_requesters = []

		self.queriers = []
		self.vtep_ports = {}
		self.reg_ports = {}
		self.igmp_queriers = {}

		self.unhandled = {}

	def _monitor(self):
		while True:
			hub.sleep(3)
			# list your periodic tasks here

			# for it in self.port_stats_requesters:
			# 	it[0].send_msg(it[2])
			self.monitor1.write ("-------------------------------------------")
			for k,v in self.vtep_ports.items():
				self.monitor1.write ("dp=",k)
				self.monitor1.write ("vtep ports",v)
			for k,v in self.reg_ports.items():
				self.monitor1.write ("dp=",k)
				self.monitor1.write ("reg ports",v)


	# invoked such as when a switch connects to this controller
	@set_ev_cls(ofp_event.EventOFPSwitchFeatures, CONFIG_DISPATCHER)
	def switch_features_handler(self, ev):
		dp = ev.msg.datapath
		ofproto = dp.ofproto
		parser = dp.ofproto_parser

		# initialisation for the first handshake
		if dp.id in self.initialised_switches:
			return
		else:
			self.initialised_switches.add(dp.id)

		self.mac_to_port.setdefault(dp.id, {})
		self.ip_to_port.setdefault(dp.id, {})

		# a reconnected switch might have different port setup, so reset the dict
		self.vtep_ports[dp.id] = set()
		self.reg_ports[dp.id] = set()

		# install table-miss flow entry.
		# Ryu says use OFPCML_NO_BUFFER due to bug in OVS prior to v2.1.0
		match = parser.OFPMatch()
		actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER)]
		self.add_flow(dp, 0, match, actions)

		# Always escalate control messages such as IGMP to controllers
		# priority has 16 bits, here use highest priority
		match = parser.OFPMatch(eth_type=ether_types.ETH_TYPE_IP, ip_proto=in_proto.IPPROTO_IGMP)
		actions = [parser.OFPActionOutput(ofproto.OFPP_CONTROLLER)]
		self.add_flow(dp, 2**16-1, match, actions)

		# scan available ports
		#req = parser.OFPPortStatsRequest(dp, 0, ofproto.OFPP_ANY)
		req = parser.OFPPortDescStatsRequest(dp, 0)
		#self.port_stats_requesters.append((dp,req,req2))
		dp.send_msg(req)

		self.logger.info('Switch initialised: %s', dp.id)

	@set_ev_cls(ofp_event.EventOFPErrorMsg, [HANDSHAKE_DISPATCHER, CONFIG_DISPATCHER, MAIN_DISPATCHER])
	def error_msg_handler(self, ev):
		msg = ev.msg

		self.logger.warning('OFPErrorMsg received:\n'
							'type=0x%02x %s\n'
							'code=0x%02x',
							msg.type, ofpet_no_to_text[msg.type],
							msg.code)

	@set_ev_cls(ofp_event.EventOFPPortDescStatsReply, MAIN_DISPATCHER)
	def port_desc_stats_reply_handler(self, ev):
		dp = ev.msg.datapath
		ofproto = dp.ofproto
		parser = dp.ofproto_parser

		ports = []
		for p in ev.msg.body:
			# check if such name exists in port name, just to pick out specific ports
			if ("tun" in p.name):
				self.vtep_ports[dp.id].add(p.port_no)
			else:
				self.reg_ports[dp.id].add(p.port_no)
		
		# instantiate IGMP classes (which auto-spawn threads)
		if dp.id in self.igmp_queriers:
			self.igmp_queriers[dp.id].__del__()
			# del self.igmp_queriers[dp.id]
		
		self.igmp_queriers[dp.id] = IgmpQuerier(ev, self.reg_ports[dp.id], self.vtep_ports[dp.id], 'xterm_IGMP_monitor_'+str(dp.id))
		self.igmp_queriers[dp.id] = IgmpQuerier(ev, self.reg_ports[dp.id], self.vtep_ports[dp.id], 'xterm_IGMP_monitor_'+str(dp.id))

		self.logger.info('OFPPortDescStatsReply received: %s', dp.id)


	def add_flow(self, datapath, priority, match, actions):
		ofproto = datapath.ofproto
		parser = datapath.ofproto_parser

		inst = [parser.OFPInstructionActions(ofproto.OFPIT_APPLY_ACTIONS,
											 actions)]

		mod = parser.OFPFlowMod(datapath=datapath, priority=priority,
								match=match, instructions=inst)
		datapath.send_msg(mod)


	@set_ev_cls(ofp_event.EventOFPPacketIn, MAIN_DISPATCHER)
	def _packet_in_handler(self, ev):
		msg = ev.msg
		dp = msg.datapath
		ofproto = dp.ofproto
		parser = dp.ofproto_parser
		in_port = msg.match['in_port']

		pkt = packet.Packet(msg.data)
		eth = pkt.get_protocol(ethernet.ethernet)

		
		# intercept LLDP and discard
		if eth.ethertype == ether_types.ETH_TYPE_LLDP:
			return
		
		# Inspect IPv4
		if eth.ethertype == ether_types.ETH_TYPE_IP:
			ipv4_header = pkt.get_protocol(ipv4.ipv4)

			# Intercept IGMP
			if (ipv4_header.proto == in_proto.IPPROTO_IGMP):
				self.igmp_queriers[dp.id].dispatcher(ev)
				return

			# Intercept IPv4 Multicasting without flows & Discard
			if ((ip.text_to_int(ipv4_header.dst)>>28) == 0xE):
				self.logger.debug('discarding multicasting: %s', ipv4_header.dst)
				return
		
		# intercept non-IP and discard for debugging IGMP
		else:
			self.unhandled.setdefault(eth.ethertype, 0)

			self.unhandled[eth.ethertype] += 1

			# display discarded
			self.logger.debug('-----------------------')
			for k,v in self.unhandled.items():
				self.logger.debug('%s: %d', ethertype_bits_to_name[k], v)
			
			# discard!!!!!!!!!!!!!!!!!!!!!
			# return



		# Default mode, basic layer2 switching
		self._layer2_switching(ev, eth)


	def _layer2_switching(self, ev, eth):
		msg = ev.msg
		dp = msg.datapath
		ofproto = dp.ofproto
		parser = dp.ofproto_parser
		in_port = msg.match['in_port']

		dst = eth.dst
		src = eth.src

		self.logger.debug("packet in dp:%s port:%s type:%s\nsrc:%s dst:%s", 
			dp.id, in_port, ethertype_bits_to_name[eth.ethertype],
			src, dst)

		# learn a mac address to avoid FLOOD next time.
		self.mac_to_port[dp.id][src] = in_port

		if dst in self.mac_to_port[dp.id]:
			out_port = self.mac_to_port[dp.id][dst]
		else:
			out_port = ofproto.OFPP_FLOOD

		actions = [parser.OFPActionOutput(out_port)]

		# install a flow to avoid packet_in next time
		if out_port != ofproto.OFPP_FLOOD:
			match = parser.OFPMatch(in_port=in_port, eth_dst=dst, eth_src=src)
			self.add_flow(dp, 1, match, actions)

		# prevent flooding messaging from one vtep port to other vtep ports
		if (in_port in self.vtep_ports[dp.id] and out_port == ofproto.OFPP_FLOOD):
			self.logger.debug("flood packet at dp:%s in from port:%s", dp.id, in_port)
			actions = [parser.OFPActionOutput(port_it) for port_it in self.reg_ports[dp.id]]

		data = None
		if msg.buffer_id == ofproto.OFP_NO_BUFFER:
			data = msg.data

		out = parser.OFPPacketOut(datapath=dp, buffer_id=msg.buffer_id,
								  in_port=in_port, actions=actions, data=data)

		dp.send_msg(out)
