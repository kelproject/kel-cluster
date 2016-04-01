import hashlib
import logging
import os
import time

import googleapiclient.errors

from googleapiclient.discovery import build
from oauth2client.client import GoogleCredentials


logger = logging.getLogger(__name__)


def build_gce_compute():
    credentials = GoogleCredentials.get_application_default()
    return build("compute", "v1", credentials=credentials)


class GCE:

    def __init__(self, project, region, zone, compute):
        self.project = project
        self.region = region
        self.zone = zone
        self.compute = compute

    def global_kwargs(self, **kwargs):
        kwargs["project"] = self.project
        return kwargs

    def region_kwargs(self, **kwargs):
        kwargs["region"] = self.region
        return self.global_kwargs(**kwargs)

    def zone_kwargs(self, **kwargs):
        kwargs["zone"] = self.zone
        return self.global_kwargs(**kwargs)

    def global_wait(self, op):
        while True:
            result = self.compute.globalOperations().get(**self.global_kwargs(operation=op["name"])).execute()
            if result["status"] == "DONE":
                if "error" in result:
                    raise Exception(result["error"])
                return result
            else:
                time.sleep(1)

    def region_wait(self, op):
        while True:
            result = self.compute.regionOperations().get(**self.region_kwargs(operation=op["name"])).execute()
            if result["status"] == "DONE":
                if "error" in result:
                    raise Exception(result["error"])
                return result
            else:
                time.sleep(1)

    def zone_wait(self, op):
        while True:
            result = self.compute.zoneOperations().get(**self.zone_kwargs(operation=op["name"])).execute()
            if result["status"] == "DONE":
                if "error" in result:
                    raise Exception(result["error"])
                return result
            else:
                time.sleep(1)

    def create_disk(self, name, size, kind):
        try:
            self.compute.disks().get(**self.zone_kwargs(disk=name)).execute()
        except googleapiclient.errors.HttpError as e:
            if e.resp.status == 404:
                body = {
                    "name": name,
                    "sizeGb": str(size),
                    "type": "zones/{}/diskTypes/{}".format(
                        self.zone,
                        kind,
                    ),
                }
                op = self.compute.disks().insert(**self.zone_kwargs(body=body)).execute()
                self.zone_wait(op)
                logger.info('created disk "{}"'.format(name))
        else:
            logger.info('disk "{}" already exists'.format(name))

    def destroy_disk(self, name):
        pass

    def _create_target_pool(self, name):
        try:
            self.compute.targetPools().get(**self.region_kwargs(targetPool=name)).execute()
        except googleapiclient.errors.HttpError as e:
            if e.resp.status == 404:
                body = {
                    "name": name,
                }
                op = self.compute.targetPools().insert(**self.region_kwargs(body=body)).execute()
                self.region_wait(op)
                logger.info('created target pool "{}"'.format(name))
                return self.compute.targetPools().get(**self.region_kwargs(targetPool=name)).execute()
        else:
            logger.info('target pool "{}" already exists'.format(name))

    def _create_forwarding_rule(self, name, target, ports, ip=None):
        try:
            self.compute.forwardingRules().get(**self.region_kwargs(forwardingRule=name)).execute()
        except googleapiclient.errors.HttpError as e:
            if e.resp.status == 404:
                body = {
                    "name": name,
                    "IPProtocol": "TCP",
                    "portRange": "{}-{}".format(min(ports), max(ports)),
                    "target": target["selfLink"],
                }
                if ip is not None:
                    body["IPAddress"] = ip
                op = self.compute.forwardingRules().insert(**self.region_kwargs(body=body)).execute()
                self.region_wait(op)
                logger.info('created forwarding rule "{}"'.format(name))
                return self.compute.forwardingRules().get(**self.region_kwargs(forwardingRule=name)).execute()
        else:
            logger.info('forwarding rule "{}" already exists'.format(name))

    def create_loadbalancer(self, name, ports, ip=None):
        tp = self._create_target_pool("{}-pool".format(name))
        self._create_forwarding_rule("{}-rule".format(name), tp, ports, ip=ip)

    def destroy_loadbalancer(self, name):
        self._destroy_forwarding_rule("{}-rule".format(name))
        self._destroy_target_pool("{}-pool".format(name))

    def _destroy_target_pool(self, name):
        op = self.compute.targetPools().delete(**self.region_kwargs(targetPool=name)).execute()
        self.region_wait(op)
        logger.info('destroyed target pool "{}"'.format(self.name))

    def _destroy_forwarding_rule(self, name):
        op = self.compute.forwardingRules().delete(**self.region_kwargs(forwardingRule=name)).execute()
        self.region_wait(op)
        logger.info('destroyed forwarding rule "{}"'.format(name))


def setup(**kwargs):
    return GCE(
        project=kwargs["project-id"],
        region=kwargs["region"],
        zone=kwargs["zone"],
        compute=build_gce_compute(),
    )


class GCEResource:

    def __init__(self, provider, cluster, config, **kwargs):
        self.provider = provider
        self.compute = provider.compute
        self.cluster = cluster
        self.config = config.copy()
        self.project = provider.project
        self.region = provider.region
        self.zone = provider.zone

        self.set_default_config()
        self.metadata = {}

        self.cluster.resources.setdefault(self.get_name(), self)

    def set_default_config(self):
        pass

    def get_name(self):
        return self.name

    def global_kwargs(self, **kwargs):
        return self.provider.global_kwargs(**kwargs)

    def region_kwargs(self, **kwargs):
        return self.provider.region_kwargs(**kwargs)

    def zone_kwargs(self, **kwargs):
        return self.provider.zone_kwargs(**kwargs)

    def get_fqdn(self, name):
        return "{}.c.{}.internal".format(name, self.project)

    def get_source_image(self):
        if self.cluster.config["layers"]["os"]["type"] == "coreos":
            resp = self.compute.images().list(project="coreos-cloud").execute()
            for image in resp["items"]:
                name = "coreos-{}-{}".format(
                    self.cluster.config["layers"]["os"]["channel"],
                    self.cluster.config["layers"]["os"]["version"].replace(".", "-"),
                )
                if image["name"].startswith(name):
                    break
            else:
                raise Exception("cannot find CoreOS image")
            return "projects/{}/global/images/{}".format("coreos-cloud", image["name"])
        else:
            raise Exception("unsupported OS type")

    @property
    def template_hash(self):
        return hashlib.sha1(b"".join([
            self.cluster.config["layers"]["os"]["type"].encode("utf-8"),
            self.cluster.config["layers"]["os"]["channel"].encode("utf-8"),
            self.cluster.config["layers"]["os"]["version"].encode("utf-8"),
            self.cluster.config["layers"]["kubernetes"]["version"].encode("utf-8"),
        ])).hexdigest()[:8]

    def get_hashed_template_name(self, name):
        return "{}-{}-{}".format(
            self.cluster.config["name"],
            name,
            self.template_hash,
        )

    def global_wait(self, op):
        self.provider.global_wait(op)

    def region_wait(self, op):
        self.provider.region_wait(op)

    def zone_wait(self, op):
        self.provider.zone_wait(op)

    def create(self):
        raise NotImplementedError()

    def destroy(self):
        raise NotImplementedError()


class Network(GCEResource):

    name = "network"

    def set_default_config(self):
        self.config.setdefault("name", self.cluster.config["name"])

    def create_network(self):
        body = {
            "name": self.config["name"],
            "IPv4Range": self.config["ipv4-range"],
        }
        op = self.compute.networks().insert(**self.global_kwargs(body=body)).execute()
        self.global_wait(op)
        logger.info('created network "{}"'.format(self.config["name"]))
        return self.compute.networks().get(**self.global_kwargs(network=body["name"])).execute()

    def create_firewall(self, name, body):
        body.update({
            "name": "{}-{}".format(self.metadata["network"]["name"], name),
            "network": self.metadata["network"]["selfLink"],
        })
        op = self.compute.firewalls().insert(**self.global_kwargs(body=body)).execute()
        self.global_wait(op)
        logger.info('created firewall "{}" on network "{}"'.format(body["name"], self.config["name"]))
        return self.compute.firewalls().get(**self.global_kwargs(firewall=body["name"])).execute()

    def create(self):
        network = self.create_network()
        self.metadata["network"] = network
        self.create_firewall(
            "allow-icmp",
            {
                "allowed": [
                    {
                        "IPProtocol": "icmp",
                    },
                ],
                "sourceRanges": [
                    "0.0.0.0/0",
                ]
            },
        )
        self.create_firewall(
            "allow-internal",
            {
                "allowed": [
                    {
                        "IPProtocol": "tcp",
                        "ports": [
                            "1-65535",
                        ],
                    },
                    {
                        "IPProtocol": "udp",
                        "ports": [
                            "1-65535",
                        ],
                    },
                ],
                "sourceRanges": [
                    self.config["ipv4-range"],
                ]
            },
        )
        self.create_firewall(
            "allow-podnet",
            {
                "allowed": [
                    {
                        "IPProtocol": "tcp",
                        "ports": [
                            "1-65535",
                        ],
                    },
                    {
                        "IPProtocol": "udp",
                        "ports": [
                            "1-65535",
                        ],
                    },
                    {
                        "IPProtocol": "icmp",
                    },
                ],
                "sourceRanges": [
                    self.cluster.config["pod-network"],
                ]
            },
        )
        self.create_firewall(
            "allow-ssh",
            {
                "allowed": [
                    {
                        "IPProtocol": "tcp",
                        "ports": [
                            "22",
                        ],
                    },
                ],
                "sourceRanges": [
                    "0.0.0.0/0",
                ]
            },
        )
        self.create_firewall(
            "allow-master-https",
            {
                "allowed": [
                    {
                        "IPProtocol": "tcp",
                        "ports": [
                            "443",
                        ],
                    },
                ],
                "sourceRanges": [
                    "0.0.0.0/0",
                ],
                "targetTags": [
                    "{}-master".format(self.config["name"]),
                ],
            },
        )
        self.create_firewall(
            "allow-router",
            {
                "allowed": [
                    {
                        "IPProtocol": "tcp",
                        "ports": [
                            "80",
                            "443",
                        ],
                    },
                ],
                "sourceRanges": [
                    "0.0.0.0/0",
                ],
                "targetTags": [
                    "{}-nodes".format(self.config["name"]),
                ],
            },
        )

    def destroy_network(self):
        op = self.compute.networks().delete(**self.global_kwargs(network=self.config["name"])).execute()
        self.global_wait(op)
        logger.info('destroyed network "{}"'.format(self.config["name"]))

    def destroy_firewall(self, name):
        name = "{}-{}".format(self.config["name"], name)
        op = self.compute.firewalls().delete(**self.global_kwargs(firewall=name)).execute()
        self.global_wait(op)
        logger.info('destroyed firewall "{}" on network "{}"'.format(name, self.config["name"]))

    def destroy_routes(self):
        resp = self.compute.routes().list(**self.global_kwargs()).execute()
        for route in resp["items"]:
            if route["network"] == self.metadata["network"]["selfLink"] and not route["name"].startswith("default-"):
                op = self.compute.routes().delete(**self.global_kwargs(route=route["name"])).execute()
                self.global_wait(op)
                logger.info('destroyed route "{}" on network "{}"'.format(route["name"], self.config["name"]))

    def destroy(self):
        self.destroy_routes()
        self.destroy_firewall("allow-icmp")
        self.destroy_firewall("allow-internal")
        self.destroy_firewall("allow-podnet")
        self.destroy_firewall("allow-ssh")
        self.destroy_firewall("allow-master-https")
        self.destroy_firewall("allow-router")
        self.destroy_network()


class EtcdCluster(GCEResource):

    name = "etcd"

    def __init__(self, *args, **kwargs):
        super(EtcdCluster, self).__init__(*args, **kwargs)
        self.metadata.update({
            "disks": {},
            "machines": {},
        })

    def node_iterator(self):
        return range(1, self.config["count"] + 1)

    def get_node_name(self, i):
        return "{}-etcd-{}".format(self.cluster.config["name"], i)

    def get_node_fqdn(self, i):
        return self.get_fqdn(self.get_node_name(i))

    def get_initial_nodes(self):
        return [
            "{}=http://{}:2380".format(
                self.get_node_name(i),
                self.get_node_fqdn(i),
            )
            for i in self.node_iterator()
        ]

    def get_initial_endpoints(self):
        return [
            "http://{}:2379".format(self.get_node_fqdn(i))
            for i in self.node_iterator()
        ]

    def create_disk(self, i):
        body = {
            "name": "{}-pd".format(self.get_node_name(i)),
            "sizeGb": str(self.config["machine"]["data-disk"]["size"]),
            "type": "zones/{}/diskTypes/{}".format(
                self.zone,
                self.config["machine"]["data-disk"]["type"],
            ),
        }
        op = self.compute.disks().insert(**self.zone_kwargs(body=body)).execute()
        self.zone_wait(op)
        logger.info('created disk "{}"'.format(body["name"]))
        disk = self.compute.disks().get(**self.zone_kwargs(disk=body["name"])).execute()
        self.metadata["disks"][i] = disk

    def destroy_disk(self, i):
        name = "{}-pd".format(self.get_node_name(i))
        op = self.compute.disks().delete(**self.zone_kwargs(disk=name)).execute()
        self.zone_wait(op)
        logger.info('destroyed disk "{}"'.format(name))

    def create_machine(self, i):
        body = {
            "name": self.get_node_name(i),
            "canIpForward": True,
            "machineType": "zones/{}/machineTypes/{}".format(
                self.zone,
                self.config["machine"]["type"],
            ),
            "disks": [
                {
                    "initializeParams": {
                        "diskSizeGb": str(self.config["machine"]["boot-disk-size"]),
                        "diskType": "zones/{}/diskTypes/{}".format(
                            self.zone,
                            self.config["machine"]["boot-disk-type"],
                        ),
                        "sourceImage": self.get_source_image(),
                    },
                    "autoDelete": True,
                    "boot": True,
                },
                {
                    "source": self.metadata["disks"][i]["selfLink"],
                    "deviceName": "etcd-pd",
                },
            ],
            "networkInterfaces": [
                {
                    "network": self.cluster.resources["network"].metadata["network"]["selfLink"],
                    "accessConfigs": [
                        {
                            "name": "External NAT",
                            "type": "ONE_TO_ONE_NAT",
                        },
                    ],
                },
            ],
            "serviceAccounts": [
                {
                    "email": "default",
                    "scopes": [
                        "https://www.googleapis.com/auth/devstorage.read_only",
                        "https://www.googleapis.com/auth/compute",
                        "https://www.googleapis.com/auth/logging.write",
                        "https://www.googleapis.com/auth/monitoring",
                    ],
                },
            ],
            "metadata": {
                "items": [
                    {
                        "key": "startup-script",
                        "value": self.cluster.decode_manifest(
                            self.cluster.config["layers"]["os"]["manifests"]["etcd"],
                            {
                                "etcd": self,
                                "i": i,
                            },
                        ),
                    },
                ],
            },
            "tags": {
                "items": [
                    self.cluster.config["name"],
                    "{}-etcd".format(self.cluster.config["name"]),
                ],
            },
        }
        op = self.compute.instances().insert(**self.zone_kwargs(body=body)).execute()
        self.zone_wait(op)
        logger.info('created machine "{}"'.format(body["name"]))
        machine = self.compute.instances().get(**self.zone_kwargs(instance=body["name"])).execute()
        self.metadata["machines"][i] = machine

    def destroy_machine(self, i):
        name = self.get_node_name(i)
        op = self.compute.instances().delete(**self.zone_kwargs(instance=name)).execute()
        self.zone_wait(op)
        logger.info('destroyed machine "{}"'.format(name))

    def create(self):
        for i in self.node_iterator():
            self.create_disk(i)
            self.create_machine(i)

    def destroy(self):
        for i in self.node_iterator():
            self.destroy_machine(i)
            self.destroy_disk(i)


class MasterGroup(GCEResource):

    name = "master"

    def set_default_config(self):
        self.config.setdefault("template-name", self.get_hashed_template_name("masters"))
        self.config.setdefault("group-name", "{}-masters".format(self.cluster.config["name"]))
        self.config.setdefault("group-base-name", "{}-master".format(self.cluster.config["name"]))

    @property
    def target_pool_name(self):
        return "{}-pool".format(self.config["group-name"])

    @property
    def forwarding_rule_name(self):
        return "{}-rule".format(self.config["group-name"])

    @property
    def instance_template_name(self):
        return self.config["template-name"]

    @property
    def instance_group_name(self):
        return self.config["group-name"]

    @property
    def instance_group_base_name(self):
        return self.config["group-base-name"]

    def create_target_pool(self):
        body = {
            "name": self.target_pool_name,
        }
        op = self.compute.targetPools().insert(**self.region_kwargs(body=body)).execute()
        self.region_wait(op)
        logger.info('created target pool "{}"'.format(self.target_pool_name))
        target_pool = self.compute.targetPools().get(**self.region_kwargs(targetPool=self.target_pool_name)).execute()
        self.metadata["target_pool"] = target_pool

    def create_forwarding_rule(self):
        body = {
            "name": self.forwarding_rule_name,
            "IPProtocol": "TCP",
            "portRange": "443",
            "target": self.metadata["target_pool"]["selfLink"],
        }
        if self.cluster.config.get("master-ip"):
            body["IPAddress"] = self.cluster.config["master-ip"]
        op = self.compute.forwardingRules().insert(**self.region_kwargs(body=body)).execute()
        self.region_wait(op)
        logger.info('created forwarding rule "{}"'.format(self.forwarding_rule_name))
        forwarding_rule = self.compute.forwardingRules().get(**self.region_kwargs(forwardingRule=self.forwarding_rule_name)).execute()
        self.metadata["forwarding_rule"] = forwarding_rule
        if not self.cluster.config.get("master-ip"):
            self.cluster.config["master-ip"] = forwarding_rule["IPAddress"]

    def create_loadbalancer(self):
        self.create_target_pool()
        self.create_forwarding_rule()

    def create_instance_template(self):
        body = {
            "name": self.instance_template_name,
            "properties": {
                "canIpForward": True,
                "machineType": self.config["machine-group"]["type"],
                "disks": [
                    {
                        "initializeParams": {
                            "diskSizeGb": str(self.config["machine-group"]["boot-disk-size"]),
                            "diskType": self.config["machine-group"]["boot-disk-type"],
                            "sourceImage": self.get_source_image(),
                        },
                        "autoDelete": True,
                        "boot": True,
                    },
                ],
                "networkInterfaces": [
                    {
                        "network": self.cluster.resources["network"].metadata["network"]["selfLink"],
                        "accessConfigs": [
                            {
                                "name": "External NAT",
                                "type": "ONE_TO_ONE_NAT",
                            },
                        ],
                    },
                ],
                "serviceAccounts": [
                    {
                        "email": "default",
                        "scopes": [
                            "https://www.googleapis.com/auth/devstorage.read_only",
                            "https://www.googleapis.com/auth/compute",
                            "https://www.googleapis.com/auth/logging.write",
                            "https://www.googleapis.com/auth/monitoring",
                        ],
                    },
                ],
                "metadata": {
                    "items": [
                        {
                            "key": "startup-script",
                            "value": self.cluster.decode_manifest(
                                self.cluster.config["layers"]["os"]["manifests"]["master"],
                                ctx={"config": self.config},
                            ),
                        },
                    ],
                },
                "tags": {
                    "items": [
                        self.cluster.config["name"],
                        "{}-master".format(self.cluster.config["name"]),
                    ],
                },
            },
        }
        op = self.compute.instanceTemplates().insert(**self.global_kwargs(body=body)).execute()
        self.global_wait(op)
        logger.info('created instance template "{}"'.format(self.instance_template_name))
        instance_template = self.compute.instanceTemplates().get(**self.global_kwargs(instanceTemplate=self.instance_template_name)).execute()
        self.metadata["instance_template"] = instance_template

    def create_instance_group(self):
        body = {
            "name": self.instance_group_name,
            "targetSize": self.config["machine-group"]["count"],
            "baseInstanceName": self.instance_group_base_name,
            "instanceTemplate": self.metadata["instance_template"]["selfLink"],
            "targetPools": [
                self.metadata["target_pool"]["selfLink"],
            ],
        }
        op = self.compute.instanceGroupManagers().insert(**self.zone_kwargs(body=body)).execute()
        self.zone_wait(op)
        while True:
            resp = self.compute.instanceGroupManagers().listManagedInstances(**self.zone_kwargs(instanceGroupManager=self.instance_group_name)).execute()
            done = True
            for instance in resp["managedInstances"]:
                logger.debug(
                    "instance {}; currentAction = {}".format(
                        os.path.basename(instance["instance"]),
                        instance["currentAction"],
                    ),
                )
                if instance["currentAction"] != "NONE":
                    done = False
            if done:
                break
            time.sleep(1)
        logger.info('created instance group manager "{}"'.format(self.instance_group_name))

    def create(self):
        self.create_loadbalancer()
        self.create_instance_template()
        self.create_instance_group()

    def destroy(self):
        self.destroy_instance_group()
        self.destroy_instance_template()
        self.destroy_loadbalancer()

    def destroy_loadbalancer(self):
        self.destroy_forwarding_rule()
        self.destroy_target_pool()

    def destroy_target_pool(self):
        op = self.compute.targetPools().delete(**self.region_kwargs(targetPool=self.target_pool_name)).execute()
        self.region_wait(op)
        logger.info('destroyed target pool "{}"'.format(self.target_pool_name))

    def destroy_forwarding_rule(self):
        op = self.compute.forwardingRules().delete(**self.region_kwargs(forwardingRule=self.forwarding_rule_name)).execute()
        self.region_wait(op)
        logger.info('destroyed forwarding rule "{}"'.format(self.forwarding_rule_name))

    def destroy_instance_template(self):
        op = self.compute.instanceTemplates().delete(**self.global_kwargs(instanceTemplate=self.instance_template_name)).execute()
        self.global_wait(op)
        logger.info('destroyed instance template "{}"'.format(self.instance_template_name))

    def destroy_instance_group(self):
        op = self.compute.instanceGroupManagers().delete(**self.zone_kwargs(instanceGroupManager=self.instance_group_name)).execute()
        self.zone_wait(op)
        logger.info('destroyed instance group manager "{}"'.format(self.instance_group_name))


class NodeGroup(GCEResource):

    def get_name(self):
        return "{}-nodes".format(self.config["name"])

    def set_default_config(self):
        self.config.setdefault("template-name", self.get_hashed_template_name(self.config["name"]))
        self.config.setdefault("group-name", "{}-{}-nodes".format(self.cluster.config["name"], self.config["name"]))
        self.config.setdefault("group-base-name", "{}-{}-node".format(self.cluster.config["name"], self.config["name"]))

    @property
    def instance_template_name(self):
        return self.config["template-name"]

    @property
    def instance_group_name(self):
        return self.config["group-name"]

    @property
    def instance_group_base_name(self):
        return self.config["group-base-name"]

    def create_instance_template(self):
        body = {
            "name": self.instance_template_name,
            "properties": {
                "canIpForward": True,
                "machineType": self.config["machine-group"]["type"],
                "disks": [
                    {
                        "initializeParams": {
                            "diskSizeGb": str(self.config["machine-group"]["boot-disk-size"]),
                            "diskType": self.config["machine-group"]["boot-disk-type"],
                            "sourceImage": self.get_source_image(),
                        },
                        "autoDelete": True,
                        "boot": True,
                    },
                ],
                "networkInterfaces": [
                    {
                        "network": self.cluster.resources["network"].metadata["network"]["selfLink"],
                        "accessConfigs": [
                            {
                                "name": "External NAT",
                                "type": "ONE_TO_ONE_NAT",
                            },
                        ],
                    },
                ],
                "serviceAccounts": [
                    {
                        "email": "default",
                        "scopes": [
                            "https://www.googleapis.com/auth/devstorage.read_only",
                            "https://www.googleapis.com/auth/compute",
                            "https://www.googleapis.com/auth/logging.write",
                            "https://www.googleapis.com/auth/monitoring",
                        ],
                    },
                ],
                "metadata": {
                    "items": [
                        {
                            "key": "startup-script",
                            "value": self.cluster.decode_manifest(
                                self.cluster.config["layers"]["os"]["manifests"]["node"],
                                ctx={"config": self.config},
                            ),
                        },
                    ],
                },
                "tags": {
                    "items": [
                        self.cluster.config["name"],
                        "{}-nodes".format(self.cluster.config["name"]),
                    ],
                },
            },
        }
        op = self.compute.instanceTemplates().insert(**self.global_kwargs(body=body)).execute()
        self.global_wait(op)
        logger.info('created instance template "{}"'.format(self.instance_template_name))
        instance_template = self.compute.instanceTemplates().get(**self.global_kwargs(instanceTemplate=self.instance_template_name)).execute()
        self.metadata["instance_template"] = instance_template

    def create_instance_group(self):
        body = {
            "name": self.instance_group_name,
            "targetSize": self.config["machine-group"]["count"],
            "baseInstanceName": self.instance_group_base_name,
            "instanceTemplate": self.metadata["instance_template"]["selfLink"],
        }
        op = self.compute.instanceGroupManagers().insert(**self.zone_kwargs(body=body)).execute()
        self.zone_wait(op)
        while True:
            resp = self.compute.instanceGroupManagers().listManagedInstances(**self.zone_kwargs(instanceGroupManager=self.instance_group_name)).execute()
            done = True
            for instance in resp["managedInstances"]:
                logger.debug(
                    "instance {}; currentAction = {}".format(
                        os.path.basename(instance["instance"]),
                        instance["currentAction"],
                    ),
                )
                if instance["currentAction"] != "NONE":
                    done = False
            if done:
                break
            time.sleep(1)
        logger.info('created instance group manager "{}"'.format(self.instance_group_name))

    def create(self):
        self.create_instance_template()
        self.create_instance_group()

    def destroy(self):
        self.destroy_instance_group()
        self.destroy_instance_template()

    def destroy_instance_template(self):
        op = self.compute.instanceTemplates().delete(**self.global_kwargs(instanceTemplate=self.instance_template_name)).execute()
        self.global_wait(op)
        logger.info('destroyed instance template "{}"'.format(self.instance_template_name))

    def destroy_instance_group(self):
        op = self.compute.instanceGroupManagers().delete(**self.zone_kwargs(instanceGroupManager=self.instance_group_name)).execute()
        self.zone_wait(op)
        logger.info('destroyed instance group manager "{}"'.format(self.instance_group_name))