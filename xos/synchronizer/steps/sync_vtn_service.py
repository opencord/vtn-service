
# Copyright 2017-present Open Networking Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


import os
import requests
import socket
import sys
import base64
from synchronizers.vtn.vtnnetport import VTNNetwork, VTNPort
from synchronizers.new_base.syncstep import SyncStep
from synchronizers.new_base.modelaccessor import *
from requests.auth import HTTPBasicAuth

from xosconfig import Config
from multistructlog import create_logger

log = create_logger(Config().get('logging'))


# XXX should save and load this
glo_saved_networks = {}
glo_saved_ports = {}

class SyncVTNService(SyncStep):
    provides=[Service]
    observes=Service
    requested_interval=0

    def __init__(self, **args):
        SyncStep.__init__(self, **args)

    def get_vtn_onos_app(self, vtn_service):
        links = vtn_service.subscribed_links.all()
        for link in links:
            # We're looking for an ONOS App. It's the only ServiceInstance that VTN can be implemented on.
            if link.provider_service_instance.leaf_model_name != "ONOSApp":
                continue

            # TODO: Rather than checking model name, check for the right interface
            # NOTE: Deferred until service interfaces are revisited

            #if not link.provider_service_interface:
            #    log.warning("Link %s does not have a provider_service_interface. Skipping" % link)
            #    continue
            #
            #if link.provider_service_interface.interface_type.name != "onos_app_interface":
            #    log.warning("Link %s provider_service_interface type is not equal to onos_app_interface" % link)
            #    continue

            # cast from ServiceInstance to ONOSApp
            app = link.provider_service_instance.leaf_model
            return app

        raise Exception("No ServiceInstanceLink from VTN Service to VTN ONOS App")

    def get_vtn_endpoint(self, vtn_service):
        """ Get connection info for the ONOS that is hosting the VTN ONOS App.

            returns (hostname, port, auth)
        """
        app = self.get_vtn_onos_app(vtn_service)
        # cast from Service to ONOSService
        onos = app.owner.leaf_model
        if not (onos.rest_hostname):
            raise Exception("onos.rest_hostname is not set")
        if not (onos.rest_port):
            raise Exception("onos.rest_port is not set")
        if not (onos.rest_password):
            raise Exception("onos.rest_password is not set")
        if not (onos.rest_username):
            raise Exception("onos.rest_username is not set")
        auth = HTTPBasicAuth(onos.rest_username, onos.rest_password)
        return (onos.rest_hostname, onos.rest_port, auth)

    def get_method(self, auth, url, id):
        url_with_id = "%s/%s" % (url, id)
        r = requests.get(url_with_id, auth=auth)
        if (r.status_code==200):
            method="PUT"
            url = url_with_id
            req_func = requests.put
            exists=True
        else:
            method="POST"
            req_func = requests.post
            exists=False
        return (exists, url, method, req_func)

    def sync_service_networks(self, vtn_service):
        (onos_hostname, onos_port, onos_auth) = self.get_vtn_endpoint(vtn_service)

        valid_ids = []
        for network in Network.objects.all():
            network = VTNNetwork(network)

            if not network.id:
                log.info("Skipping network because it has no id")
                continue

            if (not network.subnet):
                log.info("Skipping network %s because it has no subnet" % network.id)
                continue

            if (not network.segmentation_id):
                log.info("Skipping network %s because it has no segmentation id" % network.id)
                continue

            valid_ids.append(network.id)

            # clean the providerNetworks list to what the VTN App expects
            providerNetworks = [{"id": x["id"], "bidirectional": x["bidirectional"]} for x in network.providerNetworks]

            # This is the data we will be sending ONOS
            data = {"ServiceNetwork": {"id": network.id,
                                       "name": network.name,
                                       "type": network.type,
                                       "segment_id": network.segmentation_id,
                                       "subnet": network.subnet,
                                       "service_ip": network.gateway,
                                       "providerNetworks": providerNetworks}}

            if (glo_saved_networks.get(network.id, None) != data):
                (exists, url, method, req_func) = self.get_method(onos_auth, "http://%s:%d/onos/cordvtn/serviceNetworks" % (onos_hostname, onos_port), network.id)

                log.info("%sing VTN API for network %s" % (method, network.id), url=url, data=data)

                r=req_func(url, json=data, auth=onos_auth )
                if (r.status_code in [200,201]):
                    glo_saved_networks[network.id] = data
                else:
                    log.error("Received error from vtn service (%d)" % r.status_code)


        for network_id in glo_saved_networks.keys():
            if network_id not in valid_ids:
                log.info("DELETEing VTN API for network %s" % network_id)

                url = "http://%s:%d/onos/cordvtn/serviceNetworks/%s" % (onos_hostname, onos_port, network_id)
                log.info("URL: %s" % url)

                r = requests.delete(url, auth=onos_auth )
                if (r.status_code in [200,204]):
                    del glo_saved_networks[network_id]
                else:
                    log.error("Received error from vtn service (%d)" % r.status_code)

    def sync_service_ports(self, vtn_service):
        (onos_hostname, onos_port, onos_auth) = self.get_vtn_endpoint(vtn_service)

        valid_ids = []
        for port in Port.objects.all():
            port = VTNPort(port)

            if not port.id:
                log.info("Skipping port because it has no id")
                continue

            if (not port.network_id):
                log.info("Skipping port %s because it is missing network_id" % port.id)
                continue

            if (not port.mac_address):
                log.info("Skipping port %s because it is missing mac_address" % port.id)
                continue

            if (not port.ip_address):
                log.info("Skipping port %s because it is missing ip_address" % port.id)
                continue

            valid_ids.append(port.id)

            # This is the data we will be sending ONOS
            data = {"ServicePort": {"id": port.id,
                                    "name": port.name,
                                    "network_id": port.network_id,
                                    "mac_address": port.mac_address,
                                    "ip_address": port.ip_address,
                                    "floating_address_pairs": port.floating_address_pairs}}
            if port.vlan_id:
                data["ServicePort"]["vlan_id"] = port.vlan_id

            if (glo_saved_ports.get(port.id, None) != data):
                (exists, url, method, req_func) = self.get_method(onos_auth, "http://%s:%d/onos/cordvtn/servicePorts" % (onos_hostname, onos_port), port.id)

                log.info("%sing VTN API for port %s" % (method, port.id), url=url, data=data)

                r=req_func(url, json=data, auth=onos_auth )
                if (r.status_code in [200,201]):
                    glo_saved_ports[port.id] = data
                else:
                    log.error("Received error from vtn service (%d)" % r.status_code)

        for port_id in glo_saved_ports.keys():
            if port_id not in valid_ids:
                log.info("DELETEing VTN API for port %s" % port_id)

                url = "http://%s:%d/onos/cordvtn/servicePorts/%s" % (onos_hostname, onos_port, port_id)
                log.info("URL: %s" % url)

                r = requests.delete(url, auth=onos_auth )
                if (r.status_code in [200,204]):
                    del glo_saved_ports[port_id]
                else:
                    log.error("Received error from vtn service (%d)" % r.status_code)

    def call(self, **args):
        global glo_saved_networks
        global glo_saved_ports

        vtn_service = VTNService.objects.all()
        if not vtn_service:
            raise Exception("No VTN Service")

        vtn_service = vtn_service[0]

        # TODO: We should check get_vtn_onos_app() and make sure that it has been synced, and that any necessary
        #       attributes (netcfg, etc) is filled out.

        if (vtn_service.resync):
            # If the VTN app requested a full resync, clear our saved network
            # so we will resync everything, then reset the 'resync' flag
            glo_saved_networks = {}
            glo_saved_ports = {}

            vtn_service.resync = False
            vtn_service.save()

        if vtn_service.vtnAPIVersion>=2:
            # version 2 means use new API
            self.sync_service_networks(vtn_service)
            self.sync_service_ports(vtn_service)
        else:
            raise Exception("VTN API Version 1 is no longer supported by VTN Synchronizer")


