import json
import platform
import sys

from cliff.command import Command

from kayobe import ansible
from kayobe import kolla_ansible
from kayobe import utils


def _build_playbook_list(*playbooks):
    """Return a list of names of playbook files given their basenames."""
    return ["ansible/%s.yml" % playbook for playbook in playbooks]


class KayobeAnsibleMixin(object):
    """Mixin class for commands running Kayobe Ansible playbooks."""

    def get_parser(self, prog_name):
        parser = super(KayobeAnsibleMixin, self).get_parser(prog_name)
        group = parser.add_argument_group("Kayobe Ansible")
        self.add_kayobe_ansible_args(group)
        return parser

    def add_kayobe_ansible_args(self, group):
        ansible.add_args(group)


class KollaAnsibleMixin(object):
    """Mixin class for commands running Kolla Ansible."""

    def get_parser(self, prog_name):
        parser = super(KollaAnsibleMixin, self).get_parser(prog_name)
        group = parser.add_argument_group("Kolla Ansible")
        self.add_kolla_ansible_args(group)
        return parser

    def add_kolla_ansible_args(self, group):
        kolla_ansible.add_args(group)


class ControlHostBootstrap(KayobeAnsibleMixin, Command):
    """Bootstrap the Kayobe control environment."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Bootstrapping Kayobe control host")
        linux_distname = platform.linux_distribution()[0]
        if linux_distname == "CentOS Linux":
            utils.yum_install(["epel-release"])
        else:
            # On RHEL, the following should be done to install EPEL:
            # sudo subscription-manager repos --enable=qci-1.0-for-rhel-7-rpms
            # if ! yum info epel-release >/dev/null 2>&1 ; then
            #     sudo yum -y install \
            #         https://dl.fedoraproject.org/pub/epel/epel-release-latest-7.noarch.rpm
            # fi
            self.app.LOG.error("%s is not currently supported", linux_distname)
            sys.exit(1)
        utils.yum_install(["ansible"])
        utils.galaxy_install("ansible/requirements.yml", "ansible/roles")
        playbooks = _build_playbook_list("bootstrap", "kolla")
        ansible.run_playbooks(parsed_args, playbooks)


class ConfigurationDump(KayobeAnsibleMixin, Command):
    """Dump Kayobe configuration."""

    def get_parser(self, prog_name):
        parser = super(ConfigurationDump, self).get_parser(prog_name)
        group = parser.add_argument_group("Configuration Dump")
        group.add_argument("--dump-facts", default=False,
                           help="whether to gather and dump host facts")
        group.add_argument("--host",
                           help="name of a host to dump config for")
        group.add_argument("--hosts",
                           help="name of hosts and/or groups to dump config "
                                "for")
        group.add_argument("--var-name",
                           help="name of a variable to dump")
        return parser

    def take_action(self, parsed_args):
        self.app.LOG.debug("Dumping Ansible configuration")
        hostvars = ansible.config_dump(parsed_args,
                                       host=parsed_args.host,
                                       hosts=parsed_args.hosts,
                                       facts=parsed_args.dump_facts,
                                       var_name=parsed_args.var_name)
        try:
            json.dump(hostvars, sys.stdout, sort_keys=True, indent=4)
        except TypeError as e:
            self.app.LOG.error("Failed to JSON encode configuration: %s",
                               repr(e))
            sys.exit(1)


class PlaybookRun(KayobeAnsibleMixin, Command):
    """Run a Kayobe Ansible playbook."""

    def add_kayobe_ansible_args(self, group):
        super(PlaybookRun, self).add_kayobe_ansible_args(group)
        group.add_argument("playbook", nargs="+",
                           help="name of the playbook(s) to run")

    def take_action(self, parsed_args):
        self.app.LOG.debug("Running Kayobe playbook(s)")
        ansible.run_playbooks(parsed_args, parsed_args.playbook)


class KollaAnsibleRun(KollaAnsibleMixin, Command):
    """Run a Kolla Ansible command."""

    def add_kolla_ansible_args(self, group):
        super(KollaAnsibleRun, self).add_kolla_ansible_args(group)
        group.add_argument("--kolla-inventory-filename", default="overcloud",
                           choices=["seed", "overcloud"],
                           help="name of the kolla-ansible inventory file, "
                                "one of seed or overcloud (default "
                                "overcloud)")
        group.add_argument("command",
                           help="name of the kolla-ansible command to run")

    def take_action(self, parsed_args):
        self.app.LOG.debug("Running Kolla Ansible command")
        kolla_ansible.run(parsed_args, parsed_args.command,
                          parsed_args.kolla_inventory_filename)


class PhysicalNetworkConfigure(KayobeAnsibleMixin, Command):
    """Configure a set of physical network devices."""

    def get_parser(self, prog_name):
        parser = super(PhysicalNetworkConfigure, self).get_parser(
            prog_name)
        group = parser.add_argument_group("Physical Networking")
        group.add_argument("--group", required=True,
                           help="the Ansible group to apply configuration to")
        return parser

    def take_action(self, parsed_args):
        self.app.LOG.debug("Configuring a physical network")
        ansible.run_playbook(parsed_args, "ansible/physical-network.yml",
                             limit=parsed_args.group)


class SeedVMProvision(KollaAnsibleMixin, KayobeAnsibleMixin, Command):
    """Provision the seed VM."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Provisioning seed VM")
        ansible.run_playbook(parsed_args, "ansible/ip-allocation.yml",
                             limit="seed")
        ansible.run_playbook(parsed_args, "ansible/seed-vm.yml")
        # Now populate the Kolla Ansible inventory.
        ansible.run_playbook(parsed_args, "ansible/kolla-ansible.yml",
                             tags="config")


class SeedHostConfigure(KollaAnsibleMixin, KayobeAnsibleMixin, Command):
    """Configure the seed node host OS."""

    def get_parser(self, prog_name):
        parser = super(SeedHostConfigure, self).get_parser(prog_name)
        group = parser.add_argument_group("Host Configuration")
        group.add_argument("--wipe-disks", action='store_true',
                           help="wipe partition and LVM data from all disks "
                                "that are not mounted. Warning: this can "
                                "result in the loss of data")
        return parser

    def take_action(self, parsed_args):
        self.app.LOG.debug("Configuring seed host OS")
        ansible_user = ansible.config_dump(parsed_args, host="seed",
                                           var_name="kayobe_ansible_user")
        playbooks = _build_playbook_list(
            "ip-allocation", "ssh-known-host", "kayobe-ansible-user")
        if parsed_args.wipe_disks:
            playbooks += _build_playbook_list("wipe-disks")
        playbooks += _build_playbook_list(
            "dev-tools", "disable-selinux", "network", "ip-routing", "snat",
            "ntp", "lvm")
        ansible.run_playbooks(parsed_args, playbooks, limit="seed")
        kolla_ansible.run_seed(parsed_args, "bootstrap-servers",
                               extra_vars={"ansible_user": ansible_user})
        playbooks = _build_playbook_list("kolla-host", "docker")
        ansible.run_playbooks(parsed_args, playbooks, limit="seed")


class SeedServiceDeploy(KollaAnsibleMixin, KayobeAnsibleMixin, Command):
    """Deploy the seed services."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Deploying seed services")
        ansible.run_playbook(parsed_args, "ansible/kolla-bifrost.yml")
        kolla_ansible.run_seed(parsed_args, "deploy-bifrost")
        playbooks = _build_playbook_list(
            "seed-introspection-rules", "dell-switch-bmp")
        ansible.run_playbooks(parsed_args, playbooks)


class SeedContainerImageBuild(KayobeAnsibleMixin, Command):
    """Build the seed container images."""

    def get_parser(self, prog_name):
        parser = super(SeedContainerImageBuild, self).get_parser(
            prog_name)
        group = parser.add_argument_group("Container Image Build")
        group.add_argument("--push", action="store_true",
                           help="whether to push images to a registry after "
                                "building")
        group.add_argument("regex", nargs='*',
                           help="regular expression matching names of images "
                                "to build. Builds all images if unspecified")
        return parser

    def take_action(self, parsed_args):
        self.app.LOG.debug("Building seed container images")
        playbooks = _build_playbook_list(
            "kolla-build", "container-image-build")
        extra_vars = {"push_images": parsed_args.push}
        if parsed_args.regex:
            regexes = " ".join(parsed_args.regex)
            extra_vars["container_image_regexes"] = regexes
        ansible.run_playbooks(parsed_args, playbooks, limit="seed",
                              extra_vars=extra_vars)


class OvercloudInventoryDiscover(KayobeAnsibleMixin, Command):
    """Discover the overcloud inventory from the seed's Ironic service."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Discovering overcloud inventory")
        # Run the inventory discovery playbook separately, else the discovered
        # hosts will not be present in the following playbooks in which they
        # are used to populate other inventories.
        ansible.run_playbook(parsed_args,
                             "ansible/overcloud-inventory-discover.yml")
        # Now populate the Kolla Ansible and Bifrost inventories.
        ansible.run_playbook(parsed_args, "ansible/kolla-bifrost-hostvars.yml")
        ansible.run_playbook(parsed_args, "ansible/kolla-ansible.yml",
                             tags="config")


class OvercloudProvision(KayobeAnsibleMixin, Command):
    """Provision the overcloud."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Provisioning overcloud")
        self._configure_network(parsed_args)
        self._configure_bios_and_raid(parsed_args)
        self._deploy_servers(parsed_args)

    def _configure_network(self, parsed_args):
        self.app.LOG.debug("TODO: configure overcloud network")

    def _configure_bios_and_raid(self, parsed_args):
        self.app.LOG.debug("TODO: configure overcloud BIOS and RAID")

    def _deploy_servers(self, parsed_args):
        self.app.LOG.debug("Deploying overcloud servers via Bifrost")
        ansible.run_playbook(parsed_args,
                             "ansible/overcloud-provision.yml")


class OvercloudDeprovision(KayobeAnsibleMixin, Command):
    """Deprovision the overcloud."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Deprovisioning overcloud")
        ansible.run_playbook(parsed_args,
                             "ansible/overcloud-deprovision.yml")


class OvercloudHostConfigure(KollaAnsibleMixin, KayobeAnsibleMixin, Command):
    """Configure the overcloud host OS."""

    def get_parser(self, prog_name):
        parser = super(OvercloudHostConfigure, self).get_parser(prog_name)
        group = parser.add_argument_group("Host Configuration")
        group.add_argument("--wipe-disks", action='store_true',
                           help="wipe partition and LVM data from all disks "
                                "that are not mounted. Warning: this can "
                                "result in the loss of data")
        return parser

    def take_action(self, parsed_args):
        self.app.LOG.debug("Configuring overcloud host OS")
        ansible_user = ansible.config_dump(parsed_args, host="controllers[0]",
                                           var_name="kayobe_ansible_user")
        playbooks = _build_playbook_list(
            "ip-allocation", "ssh-known-host", "kayobe-ansible-user")
        if parsed_args.wipe_disks:
            playbooks += _build_playbook_list("wipe-disks")
        playbooks += _build_playbook_list(
            "dev-tools", "disable-selinux", "network", "ntp", "lvm")
        ansible.run_playbooks(parsed_args, playbooks, limit="controllers")
        kolla_ansible.run_overcloud(parsed_args, "bootstrap-servers",
                                    extra_vars={"ansible_user": ansible_user})
        playbooks = _build_playbook_list("kolla-host", "docker")
        ansible.run_playbooks(parsed_args, playbooks, limit="controllers")


class OvercloudServiceDeploy(KollaAnsibleMixin, KayobeAnsibleMixin, Command):
    """Deploy the overcloud services."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Deploying overcloud services")
        playbooks = _build_playbook_list("kolla-openstack", "swift-setup")
        ansible.run_playbooks(parsed_args, playbooks)
        for command in ["prechecks", "deploy"]:
            kolla_ansible.run_overcloud(parsed_args, command)
        # FIXME: Fudge to work around incorrect configuration path.
        extra_vars = {"node_config_directory": parsed_args.kolla_config_path}
        kolla_ansible.run_overcloud(parsed_args, "post-deploy",
                                    extra_vars=extra_vars)


class OvercloudContainerImagePull(KollaAnsibleMixin, Command):
    """Pull the overcloud container images from a registry."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Pulling overcloud container images")
        kolla_ansible.run_overcloud(parsed_args, "pull")


class OvercloudContainerImageBuild(KayobeAnsibleMixin, Command):
    """Build the overcloud container images."""

    def get_parser(self, prog_name):
        parser = super(OvercloudContainerImageBuild, self).get_parser(
            prog_name)
        group = parser.add_argument_group("Container Image Build")
        group.add_argument("--push", action="store_true",
                           help="whether to push images to a registry after "
                                "building")
        group.add_argument("regex", nargs='*',
                           help="regular expression matching names of images "
                                "to build. Builds all images if unspecified")
        return parser

    def take_action(self, parsed_args):
        self.app.LOG.debug("Building overcloud container images")
        playbooks = _build_playbook_list(
            "kolla-build", "container-image-build")
        extra_vars = {"push_images": parsed_args.push}
        if parsed_args.regex:
            regexes = " ".join(parsed_args.regex)
            extra_vars["container_image_regexes"] = regexes
        ansible.run_playbooks(parsed_args, playbooks, limit="controllers",
                              extra_vars=extra_vars)


class OvercloudPostConfigure(KayobeAnsibleMixin, Command):
    """Perform post-deployment configuration."""

    def take_action(self, parsed_args):
        self.app.LOG.debug("Performing post-deployment configuration")
        playbooks = _build_playbook_list(
            "ipa-images", "overcloud-introspection-rules",
            "overcloud-introspection-rules-dell-lldp-workaround",
            "provision-net")
        ansible.run_playbooks(parsed_args, playbooks)
