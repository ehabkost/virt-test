"""
Utility classes and functions to handle Virtual Machine creation using libvirt.

@copyright: 2011 Red Hat Inc.
"""

import time, os, logging, fcntl, re, shutil, tempfile
from autotest.client.shared import error
from autotest.client import utils
import utils_misc, virt_vm, storage, aexpect, remote, virsh, libvirt_xml
import data_dir, xml_utils


def libvirtd_restart():
    """
    Restart libvirt daemon.
    """
    try:
        utils.run("service libvirtd restart")
        logging.debug("Restarted libvirtd successfuly")
        return True
    except error.CmdError, detail:
        logging.error("Failed to restart libvirtd:\n%s", detail)
        return False


def libvirtd_stop():
    """
    Stop libvirt daemon.
    """
    try:
        utils.run("service libvirtd stop")
        logging.debug("Stop  libvirtd successfuly")
        return True
    except error.CmdError, detail:
        logging.error("Failed to stop libvirtd:\n%s", detail)
        return False


def libvirtd_start():
    """
    Start libvirt daemon.
    """
    try:
        utils.run("service libvirtd  start")
        logging.debug("Start  libvirtd successfuly")
        return True
    except error.CmdError, detail:
        logging.error("Failed to start libvirtd:\n%s", detail)
        return False


def service_libvirtd_control(action):
    """
    Libvirtd control by action, if cmd executes successfully,
    return True, otherwise return False.
    If the action is status, return True when it's running,
    otherwise return False.
    @ param action: start|stop|status|restart|condrestart|
      reload|force-reload|try-restart
    """
    actions = ['start','stop','restart','condrestart','reload',
               'force-reload','try-restart']
    if action in actions:
        try:
            utils.run("service libvirtd %s" % action)
            logging.debug("%s libvirtd successfuly", action)
            return True
        except error.CmdError, detail:
            logging.error("Failed to %s libvirtd:\n%s", action, detail)
            return False
    elif action == "status":
        cmd_result = utils.run("service libvirtd status", ignore_status=True)
        if re.search("pid", cmd_result.stdout.strip()):
            logging.info("Libvirtd service is running")
            return True
        else:
            return False
    else:
        raise error.TestError("Unknown action: %s" % action)


def normalize_connect_uri(connect_uri):
    """
    Processes connect_uri Cartesian into something virsh can use

    @param: connect_uri: Cartesian Params setting
    @return: Normalized connect_uri
    """
    if connect_uri == 'default':
        return None
    else: # Validate and canonicalize uri early to catch problems
        return virsh.canonical_uri(uri=connect_uri)


def complete_uri(ip_address):
    """
    Return a complete URI with the combination of ip_address and local uri.
    It is useful when you need to connect remote hypervisor.

    @param ip_address: an ip address or a hostname
    @return: a complete uri
    """
    # Allow to raise CmdError if canonical_uri is failed
    uri = virsh.canonical_uri(ignore_status=False)
    driver = uri.split(":")[0]
    # The libvirtd daemon's mode(system or session on qemu)
    daemon_mode = uri.split("/")[-1]
    complete_uri = "%s+ssh://%s/%s" % (driver, ip_address, daemon_mode)
    return complete_uri


class VM(virt_vm.BaseVM):
    """
    This class handles all basic VM operations for libvirt.
    """

    def __init__(self, name, params, root_dir, address_cache, state=None):
        """
        Initialize the object and set a few attributes.

        @param name: The name of the object
        @param params: A dict containing VM params
                (see method make_create_command for a full description)
        @param root_dir: Base directory for relative filenames
        @param address_cache: A dict that maps MAC addresses to IP addresses
        @param state: If provided, use this as self.__dict__
        """

        if state:
            self.__dict__ = state
        else:
            self.process = None
            self.serial_console = None
            self.redirs = {}
            self.vnc_port = None
            self.vnc_autoport = True
            self.pci_assignable = None
            self.netdev_id = []
            self.device_id = []
            self.pci_devices = []
            self.uuid = None
            self.only_pty = False

        self.spice_port = 8000
        self.name = name
        self.params = params
        self.root_dir = root_dir
        self.address_cache = address_cache
        self.vnclisten = "0.0.0.0"
        self.connect_uri = normalize_connect_uri( params.get("connect_uri",
                                                             "default") )
        if self.connect_uri:
            self.driver_type = virsh.driver(uri = self.connect_uri)
        else:
            self.driver_type = 'qemu'
        self.params['driver_type_'+self.name] = self.driver_type
        # virtnet init depends on vm_type/driver_type being set w/in params
        super(VM, self).__init__(name, params)
        logging.info("Libvirt VM '%s', driver '%s', uri '%s'",
                     self.name, self.driver_type, self.connect_uri)


    def verify_alive(self):
        """
        Make sure the VM is alive.

        @raise VMDeadError: If the VM is dead
        """
        if not self.is_alive():
            raise virt_vm.VMDeadError("Domain %s is inactive" % self.name,
                                      virsh.domstate(self.name,
                                                     uri=self.connect_uri))


    def is_alive(self):
        """
        Return True if VM is alive.
        """
        return virsh.is_alive(self.name, uri=self.connect_uri)


    def is_dead(self):
        """
        Return True if VM is dead.
        """
        return virsh.is_dead(self.name, uri=self.connect_uri)


    def is_persistent(self):
        """
        Return True if VM is persistent.
        """
        try:
            return bool(re.search(r"^Persistent:\s+[Yy]es",
                        virsh.dominfo(self.name, uri=self.connect_uri),
                        re.MULTILINE))
        except error.CmdError:
            return False

    def undefine(self):
        """
        Undefine the VM.
        """
        try:
            virsh.undefine(self.name, uri=self.connect_uri,
                           ignore_status=False)
        except error.CmdError, detail:
            logging.error("Undefined VM %s failed:\n%s", self.name, detail)
            return False
        return True


    def define(self, xml_file):
        """
        Define the VM.
        """
        if not os.path.exists(xml_file):
            logging.error("File %s not found." % xml_file)
            return False
        try:
            virsh.define(xml_file, uri=self.connect_uri,
                         ignore_status=False)
        except error.CmdError, detail:
            logging.error("Defined VM from %s failed:\n%s", xml_file, detail)
            return False
        return True


    def state(self):
        """
        Return domain state.
        """
        return virsh.domstate(self.name, uri=self.connect_uri)


    def get_id(self):
        """
        Return VM's ID.
        """
        return virsh.domid(self.name, uri=self.connect_uri)


    def get_xml(self):
        """
        Return VM's xml file.
        """
        return virsh.dumpxml(self.name, uri=self.connect_uri)


    def backup_xml(self):
        """
        Backup the guest's xmlfile.
        """
        # Since backup_xml() is not a function for testing,
        # we have to handle the exception here.
        try:
            xml_file = tempfile.mktemp(dir="/tmp")

            virsh.dumpxml(self.name, to_file=xml_file, uri=self.connect_uri)
            return xml_file
        except Exception, detail:
            if os.path.exists(xml_file):
                os.remove(xml_file)
            logging.error("Failed to backup xml file:\n%s", detail)
            return ""


    def clone(self, name=None, params=None, root_dir=None, address_cache=None,
              copy_state=False):
        """
        Return a clone of the VM object with optionally modified parameters.
        The clone is initially not alive and needs to be started using create().
        Any parameters not passed to this function are copied from the source
        VM.

        @param name: Optional new VM name
        @param params: Optional new VM creation parameters
        @param root_dir: Optional new base directory for relative filenames
        @param address_cache: A dict that maps MAC addresses to IP addresses
        @param copy_state: If True, copy the original VM's state to the clone.
                Mainly useful for make_create_command().
        """
        if name is None:
            name = self.name
        if params is None:
            params = self.params.copy()
        if root_dir is None:
            root_dir = self.root_dir
        if address_cache is None:
            address_cache = self.address_cache
        if copy_state:
            state = self.__dict__.copy()
        else:
            state = None
        return VM(name, params, root_dir, address_cache, state)


    def make_create_command(self, name=None, params=None, root_dir=None):
        """
        Generate a libvirt command line. All parameters are optional. If a
        parameter is not supplied, the corresponding value stored in the
        class attributes is used.

        @param name: The name of the object
        @param params: A dict containing VM params
        @param root_dir: Base directory for relative filenames

        @note: The params dict should contain:
               mem -- memory size in MBs
               cdrom -- ISO filename to use with the qemu -cdrom parameter
               extra_params -- a string to append to the qemu command
               shell_port -- port of the remote shell daemon on the guest
               (SSH, Telnet or the home-made Remote Shell Server)
               shell_client -- client program to use for connecting to the
               remote shell daemon on the guest (ssh, telnet or nc)
               x11_display -- if specified, the DISPLAY environment variable
               will be be set to this value for the qemu process (useful for
               SDL rendering)
               images -- a list of image object names, separated by spaces
               nics -- a list of NIC object names, separated by spaces

               For each image in images:
               drive_format -- string to pass as 'if' parameter for this
               image (e.g. ide, scsi)
               image_snapshot -- if yes, pass 'snapshot=on' to qemu for
               this image
               image_boot -- if yes, pass 'boot=on' to qemu for this image
               In addition, all parameters required by get_image_filename.

               For each NIC in nics:
               nic_model -- string to pass as 'model' parameter for this
               NIC (e.g. e1000)
        """
        # helper function for command line option wrappers
        def has_option(help_text, option):
            return bool(re.search(r"--%s" % option, help_text, re.MULTILINE))

        # Wrappers for all supported libvirt command line parameters.
        # This is meant to allow support for multiple libvirt versions.
        # Each of these functions receives the output of 'libvirt --help' as a
        # parameter, and should add the requested command line option
        # accordingly.

        def add_name(help_text, name):
            return " --name '%s'" % name

        def add_machine_type(help_text, machine_type):
            if has_option(help_text, "machine"):
                return " --machine %s" % machine_type
            else:
                return ""

        def add_hvm_or_pv(help_text, hvm_or_pv):
            if hvm_or_pv == "hvm":
                return " --hvm --accelerate"
            elif hvm_or_pv == "pv":
                return " --paravirt"
            else:
                logging.warning("Unknown virt type hvm_or_pv, using default.")
                return ""

        def add_mem(help_text, mem):
            return " --ram=%s" % mem

        def add_check_cpu(help_text):
            if has_option(help_text, "check-cpu"):
                return " --check-cpu"
            else:
                return ""

        def add_smp(help_text, smp):
            return " --vcpu=%s" % smp

        def add_location(help_text, location):
            if has_option(help_text, "location"):
                return " --location %s" % location
            else:
                return ""

        def add_cdrom(help_text, filename, index=None):
            if has_option(help_text, "cdrom"):
                return " --cdrom %s" % filename
            else:
                return ""

        def add_pxe(help_text):
            if has_option(help_text, "pxe"):
                return " --pxe"
            else:
                return ""

        def add_import(help_text):
            if has_option(help_text, "import"):
                return " --import"
            else:
                return ""

        def add_drive(help_text, filename, pool=None, vol=None, device=None,
                      bus=None, perms=None, size=None, sparse=False,
                      cache=None, fmt=None):
            cmd = " --disk"
            if filename:
                cmd += " path=%s" % filename
            elif pool:
                if vol:
                    cmd += " vol=%s/%s" % (pool, vol)
                else:
                    cmd += " pool=%s" % pool
            if device:
                cmd += ",device=%s" % device
            if bus:
                cmd += ",bus=%s" % bus
            if perms:
                cmd += ",%s" % perms
            if size:
                cmd += ",size=%s" % size.rstrip("Gg")
            if sparse:
                cmd += ",sparse=false"
            if fmt:
                cmd += ",format=%s" % fmt
            if cache:
                cmd += ",cache=%s" % cache
            return cmd

        def add_floppy(help_text, filename):
            return " --disk path=%s,device=floppy,ro" % filename

        def add_vnc(help_text, vnc_port=None):
            if vnc_port:
                return " --vnc --vncport=%d" % (vnc_port)
            else:
                return " --vnc"

        def add_vnclisten(help_text, vnclisten):
            if has_option(help_text, "vnclisten"):
                return " --vnclisten=%s" % (vnclisten)
            else:
                return ""

        def add_sdl(help_text):
            if has_option(help_text, "sdl"):
                return " --sdl"
            else:
                return ""

        def add_nographic(help_text):
            return " --nographics"

        def add_video(help_text, video_device):
            if has_option(help_text, "video"):
                return " --video=%s" % (video_device)
            else:
                return ""

        def add_uuid(help_text, uuid):
            if has_option(help_text, "uuid"):
                return " --uuid %s" % uuid
            else:
                return ""

        def add_os_type(help_text, os_type):
            if has_option(help_text, "os-type"):
                return " --os-type %s" % os_type
            else:
                return ""

        def add_os_variant(help_text, os_variant):
            if has_option(help_text, "os-variant"):
                return " --os-variant %s" % os_variant
            else:
                return ""

        def add_pcidevice(help_text, pci_device):
            if has_option(help_text, "host-device"):
                return " --host-device %s" % pci_device
            else:
                return ""

        def add_soundhw(help_text, sound_device):
            if has_option(help_text, "soundhw"):
                return " --soundhw %s" % sound_device
            else:
                return ""

        def add_serial(help_text, filename):
            if has_option(help_text, "serial"):
                return "  --serial file,path=%s --serial pty" % filename
            else:
                self.only_pty = True
                return ""

        def add_kernel_cmdline(help_text, cmdline):
            return " -append %s" % cmdline

        def add_connect_uri(help_text, uri):
            if uri and has_option(help_text, "connect"):
                return " --connect=%s" % uri
            else:
                return ""

        def add_nic(help_text, nic_params):
            """
            Return additional command line params based on dict-like nic_params
            """
            mac = nic_params.get('mac')
            nettype = nic_params.get('nettype')
            netdst = nic_params.get('netdst')
            nic_model = nic_params.get('nic_model')
            if nettype:
                result = " --network=%s" % nettype
            else:
                result = ""
            if has_option(help_text, "bridge"):
                # older libvirt (--network=NATdev --bridge=bridgename --mac=mac)
                if nettype != 'user':
                    result += ':%s' % netdst
                if mac: # possible to specify --mac w/o --network
                    result += " --mac=%s" % mac
            else:
                # newer libvirt (--network=mynet,model=virtio,mac=00:11)
                if nettype != 'user':
                    result += '=%s' % netdst
                if nettype and nic_model: # only supported along with nettype
                    result += ",model=%s" % nic_model
                if nettype and mac:
                    result += ',mac=%s' % mac
                elif mac: # possible to specify --mac w/o --network
                    result += " --mac=%s" % mac
            logging.debug("vm.make_create_command.add_nic returning: %s"
                             % result)
            return result

        # End of command line option wrappers

        if name is None:
            name = self.name
        if params is None:
            params = self.params
        if root_dir is None:
            root_dir = self.root_dir

        # Clone this VM using the new params
        vm = self.clone(name, params, root_dir, copy_state=True)

        virt_install_binary = utils_misc.get_path(
            root_dir,
            params.get("virt_install_binary",
                       "virt-install"))

        help_text = utils.system_output("%s --help" % virt_install_binary)

        # Find all supported machine types, so we can rule out an unsupported
        # machine type option passed in the configuration.
        hvm_or_pv = params.get("hvm_or_pv", "hvm")
        # default to 'uname -m' output
        arch_name = params.get("vm_arch_name", utils.get_current_kernel_arch())
        capabs = libvirt_xml.LibvirtXML()
        support_machine_type = capabs.os_arch_machine_map[hvm_or_pv][arch_name]
        logging.debug("Machine types supported for %s\%s: %s" % (hvm_or_pv,
                                              arch_name, support_machine_type))

        # Start constructing the qemu command
        virt_install_cmd = ""
        # Set the X11 display parameter if requested
        if params.get("x11_display"):
            virt_install_cmd += "DISPLAY=%s " % params.get("x11_display")
        # Add the qemu binary
        virt_install_cmd += virt_install_binary

        # set connect uri
        virt_install_cmd += add_connect_uri(help_text, self.connect_uri)

        # hvm or pv specificed by libvirt switch (pv used  by Xen only)
        if hvm_or_pv:
            virt_install_cmd += add_hvm_or_pv(help_text, hvm_or_pv)

        # Add the VM's name
        virt_install_cmd += add_name(help_text, name)

        machine_type = params.get("machine_type")
        if machine_type:
            if machine_type in support_machine_type:
                virt_install_cmd += add_machine_type(help_text, machine_type)
            else:
                raise error.TestNAError("Unsupported machine type %s." %
                                        (machine_type))

        mem = params.get("mem")
        if mem:
            virt_install_cmd += add_mem(help_text, mem)

        # TODO: should we do the check before we call ? negative case ?
        check_cpu = params.get("use_check_cpu")
        if check_cpu:
            virt_install_cmd += add_check_cpu(help_text)

        smp = params.get("smp")
        if smp:
            virt_install_cmd += add_smp(help_text, smp)

        # TODO: directory location for vmlinuz/kernel for cdrom install ?
        location = None
        if params.get("medium") == 'url':
            location = params.get('url')

        elif params.get("medium") == 'kernel_initrd':
            # directory location of kernel/initrd pair (directory layout must
            # be in format libvirt will recognize)
            location = params.get("image_dir")

        elif params.get("medium") == 'nfs':
            location = "nfs:%s:%s" % (params.get("nfs_server"),
                                      params.get("nfs_dir"))

        elif params.get("medium") == 'cdrom':
            if params.get("use_libvirt_cdrom_switch") == 'yes':
                virt_install_cmd += add_cdrom(help_text, params.get("cdrom_cd1"))
            elif params.get("unattended_delivery_method") == "integrated":
                virt_install_cmd += add_cdrom(help_text,
                                              os.path.join(
                                                data_dir.get_data_dir(),
                                                params.get("cdrom_unattended")
                                             ))
            else:
                location = data_dir.get_data_dir()
                kernel_dir = os.path.dirname(params.get("kernel"))
                kernel_parent_dir = os.path.dirname(kernel_dir)
                pxeboot_link = os.path.join(kernel_parent_dir, "pxeboot")
                if os.path.islink(pxeboot_link):
                    os.unlink(pxeboot_link)
                if os.path.isdir(pxeboot_link):
                    logging.info("Removed old %s leftover directory",
                                 pxeboot_link)
                    shutil.rmtree(pxeboot_link)
                os.symlink(kernel_dir, pxeboot_link)

        elif params.get("medium") == "import":
            virt_install_cmd += add_import(help_text)

        if location:
            virt_install_cmd += add_location(help_text, location)

        if params.get("display") == "vnc":
            if params.get("vnc_autoport") == "yes":
                vm.vnc_autoport = True
            else:
                vm.vnc_autoport = False
            if not vm.vnc_autoport and params.get("vnc_port"):
                vm.vnc_port = int(params.get("vnc_port"))
            virt_install_cmd += add_vnc(help_text, vm.vnc_port)
            if params.get("vnclisten"):
                vm.vnclisten = params.get("vnclisten")
            virt_install_cmd += add_vnclisten(help_text, vm.vnclisten)
        elif params.get("display") == "sdl":
            virt_install_cmd += add_sdl(help_text)
        elif params.get("display") == "nographic":
            virt_install_cmd += add_nographic(help_text)

        video_device = params.get("video_device")
        if video_device:
            virt_install_cmd += add_video(help_text, video_device)

        sound_device = params.get("sound_device")
        if sound_device:
            virt_install_cmd += add_soundhw(help_text, sound_device)

        # if none is given a random UUID will be generated by libvirt
        if params.get("uuid"):
            virt_install_cmd += add_uuid(help_text, params.get("uuid"))

        # selectable OS type
        if params.get("use_os_type") == "yes":
            virt_install_cmd += add_os_type(help_text, params.get("os_type"))

        # selectable OS variant
        if params.get("use_os_variant") == "yes":
            virt_install_cmd += add_os_variant(help_text, params.get("os_variant"))

        # Add serial console
        virt_install_cmd += add_serial(help_text, self.get_serial_console_filename())

        # If the PCI assignment step went OK, add each one of the PCI assigned
        # devices to the command line.
        if self.pci_devices:
            for pci_id in self.pci_devices:
                virt_install_cmd += add_pcidevice(help_text, pci_id)

        for image_name in params.objects("images"):
            image_params = params.object_params(image_name)
            filename = storage.get_image_filename(image_params,
                                                  data_dir.get_data_dir())
            if image_params.get("use_storage_pool") == "yes":
                filename = None
                virt_install_cmd += add_drive(help_text,
                                  filename,
                                  image_params.get("image_pool"),
                                  image_params.get("image_vol"),
                                  image_params.get("image_device"),
                                  image_params.get("image_bus"),
                                  image_params.get("image_perms"),
                                  image_params.get("image_size"),
                                  image_params.get("drive_sparse"),
                                  image_params.get("drive_cache"),
                                  image_params.get("image_format"))

            if image_params.get("boot_drive") == "no":
                continue
            if filename:
                virt_install_cmd += add_drive(help_text,
                                    filename,
                                    None,
                                    None,
                                    None,
                                    image_params.get("drive_format"),
                                    None,
                                    image_params.get("image_size"),
                                    image_params.get("drive_sparse"),
                                    image_params.get("drive_cache"),
                                    image_params.get("image_format"))

        if (params.get('unattended_delivery_method') != 'integrated' and
            not (self.driver_type == 'xen' and params.get('hvm_or_pv') == 'pv')):
            for cdrom in params.objects("cdroms"):
                cdrom_params = params.object_params(cdrom)
                iso = cdrom_params.get("cdrom")
                if params.get("use_libvirt_cdrom_switch") == 'yes':
                    # we don't want to skip the winutils iso
                    if not cdrom == 'winutils':
                        logging.debug("Using --cdrom instead of --disk for install")
                        logging.debug("Skipping CDROM:%s:%s", cdrom, iso)
                        continue
                if params.get("medium") == 'cdrom_no_kernel_initrd':
                    if iso == params.get("cdrom_cd1"):
                        logging.debug("Using cdrom or url for install")
                        logging.debug("Skipping CDROM: %s", iso)
                        continue

                if iso:
                    virt_install_cmd += add_drive(help_text,
                                      utils_misc.get_path(root_dir, iso),
                                      image_params.get("iso_image_pool"),
                                      image_params.get("iso_image_vol"),
                                      'cdrom',
                                      None,
                                      None,
                                      None,
                                      None,
                                      None,
                                      None)

        # We may want to add {floppy_otps} parameter for -fda
        # {fat:floppy:}/path/. However vvfat is not usually recommended.
        # Only support to add the main floppy if you want to add the second
        # one please modify this part.
        floppy = params.get("floppy_name")
        if floppy:
            floppy = utils_misc.get_path(data_dir.get_data_dir(), floppy)
            virt_install_cmd += add_drive(help_text, floppy,
                              None,
                              None,
                              'floppy',
                              None,
                              None,
                              None,
                              None,
                              None,
                              None)

        # setup networking parameters
        for nic in vm.virtnet:
            # make_create_command can be called w/o vm.create()
            nic = vm.add_nic(**dict(nic))
            logging.debug("make_create_command() setting up command for"
                          " nic: %s" % str(nic))
            virt_install_cmd += add_nic(help_text, nic)

        if params.get("use_no_reboot") == "yes":
            virt_install_cmd += " --noreboot"

        if params.get("use_autostart") == "yes":
            virt_install_cmd += " --autostart"

        if params.get("virt_install_debug") == "yes":
            virt_install_cmd += " --debug"

        # bz still open, not fully functional yet
        if params.get("use_virt_install_wait") == "yes":
            virt_install_cmd += (" --wait %s" %
                                 params.get("virt_install_wait_time"))

        kernel_params = params.get("kernel_params")
        if kernel_params:
            virt_install_cmd += " --extra-args '%s'" % kernel_params

        virt_install_cmd += " --noautoconsole"

        return virt_install_cmd


    @error.context_aware
    def create(self, name=None, params=None, root_dir=None, timeout=5.0,
               migration_mode=None, mac_source=None):
        """
        Start the VM by running a qemu command.
        All parameters are optional. If name, params or root_dir are not
        supplied, the respective values stored as class attributes are used.

        @param name: The name of the object
        @param params: A dict containing VM params
        @param root_dir: Base directory for relative filenames
        @param migration_mode: If supplied, start VM for incoming migration
                using this protocol (either 'tcp', 'unix' or 'exec')
        @param migration_exec_cmd: Command to embed in '-incoming "exec: ..."'
                (e.g. 'gzip -c -d filename') if migration_mode is 'exec'
        @param mac_source: A VM object from which to copy MAC addresses. If not
                specified, new addresses will be generated.

        @raise VMCreateError: If qemu terminates unexpectedly
        @raise VMKVMInitError: If KVM initialization fails
        @raise VMHugePageError: If hugepage initialization fails
        @raise VMImageMissingError: If a CD image is missing
        @raise VMHashMismatchError: If a CD image hash has doesn't match the
                expected hash
        @raise VMBadPATypeError: If an unsupported PCI assignment type is
                requested
        @raise VMPAError: If no PCI assignable devices could be assigned
        """
        error.context("creating '%s'" % self.name)
        self.destroy(free_mac_addresses=False)

        if name is not None:
            self.name = name
        if params is not None:
            self.params = params
        if root_dir is not None:
            self.root_dir = root_dir
        name = self.name
        params = self.params
        root_dir = self.root_dir

        # Verify the md5sum of the ISO images
        for cdrom in params.objects("cdroms"):
            if params.get("medium") == "import":
                break
            cdrom_params = params.object_params(cdrom)
            iso = cdrom_params.get("cdrom")
            if ((self.driver_type == 'xen') and
                (params.get('hvm_or_pv') == 'pv') and
                (os.path.basename(iso) == 'ks.iso')):
                continue
            if iso:
                iso = utils_misc.get_path(data_dir.get_data_dir(), iso)
                if not os.path.exists(iso):
                    raise virt_vm.VMImageMissingError(iso)
                compare = False
                if cdrom_params.get("md5sum_1m"):
                    logging.debug("Comparing expected MD5 sum with MD5 sum of "
                                  "first MB of ISO file...")
                    actual_hash = utils.hash_file(iso, 1048576, method="md5")
                    expected_hash = cdrom_params.get("md5sum_1m")
                    compare = True
                elif cdrom_params.get("md5sum"):
                    logging.debug("Comparing expected MD5 sum with MD5 sum of "
                                  "ISO file...")
                    actual_hash = utils.hash_file(iso, method="md5")
                    expected_hash = cdrom_params.get("md5sum")
                    compare = True
                elif cdrom_params.get("sha1sum"):
                    logging.debug("Comparing expected SHA1 sum with SHA1 sum "
                                  "of ISO file...")
                    actual_hash = utils.hash_file(iso, method="sha1")
                    expected_hash = cdrom_params.get("sha1sum")
                    compare = True
                if compare:
                    if actual_hash == expected_hash:
                        logging.debug("Hashes match")
                    else:
                        raise virt_vm.VMHashMismatchError(actual_hash,
                                                          expected_hash)

        # Make sure the following code is not executed by more than one thread
        # at the same time
        lockfile = open("/tmp/libvirt-autotest-vm-create.lock", "w+")
        fcntl.lockf(lockfile, fcntl.LOCK_EX)

        try:
            # Handle port redirections
            redir_names = params.objects("redirs")
            host_ports = utils_misc.find_free_ports(5000, 6000, len(redir_names))
            self.redirs = {}
            for i in range(len(redir_names)):
                redir_params = params.object_params(redir_names[i])
                guest_port = int(redir_params.get("guest_port"))
                self.redirs[guest_port] = host_ports[i]

            # Find available PCI devices
            self.pci_devices = []
            for device in params.objects("pci_devices"):
                self.pci_devices.append(device)

            # Find available VNC port, if needed
            if params.get("display") == "vnc":
                if params.get("vnc_autoport") == "yes":
                    self.vnc_port = None
                    self.vnc_autoport = True
                else:
                    self.vnc_port = utils_misc.find_free_port(5900, 6100)
                    self.vnc_autoport = False

            # Find available spice port, if needed
            if params.get("spice"):
                self.spice_port = utils_misc.find_free_port(8000, 8100)

            # Find random UUID if specified 'uuid = random' in config file
            if params.get("uuid") == "random":
                f = open("/proc/sys/kernel/random/uuid")
                self.uuid = f.read().strip()
                f.close()

            # Generate or copy MAC addresses for all NICs
            for nic in self.virtnet:
                nic_params = dict(nic)
                if mac_source:
                    # Will raise exception if source doesn't
                    # have cooresponding nic
                    logging.debug("Copying mac for nic %s from VM %s"
                                    % (nic.nic_name, mac_source.nam))
                    nic_params['mac'] = mac_source.get_mac_address(nic.nic_name)
                # make_create_command() calls vm.add_nic (i.e. on a copy)
                nic = self.add_nic(**nic_params)
                logging.debug('VM.create activating nic %s' % nic)
                self.activate_nic(nic.nic_name)

            # Make qemu command
            install_command = self.make_create_command()

            logging.info("Running libvirt command (reformatted):")
            for item in install_command.replace(" -", " \n    -").splitlines():
                logging.info("%s", item)
            utils.run(install_command, verbose=False)
            # Wait for the domain to be created
            utils_misc.wait_for(func=self.is_alive, timeout=60,
                                text=("waiting for domain %s to start" %
                                      self.name))
            self.uuid = virsh.domuuid(self.name, uri=self.connect_uri)

            # Establish a session with the serial console
            if self.only_pty == True:
                self.serial_console = aexpect.ShellSession(
                    "virsh console %s" % self.name,
                    auto_close=False,
                    output_func=utils_misc.log_line,
                    output_params=("serial-%s.log" % name,))
            else:
                self.serial_console = aexpect.ShellSession(
                    "tail -f %s" % self.get_serial_console_filename(),
                    auto_close=False,
                    output_func=utils_misc.log_line,
                    output_params=("serial-%s.log" % name,))

        finally:
            fcntl.lockf(lockfile, fcntl.LOCK_UN)
            lockfile.close()


    def migrate(self, dest_uri="", option="--live --timeout 60", extra="",
                ignore_status=False, debug=False):
        """
        Migrate a VM to a remote host.

        @param: dest_uri: Destination libvirt URI
        @param: option: Migration options before <domain> <desturi>
        @param: extra: Migration options after <domain> <desturi>
        @return: True if command succeeded
        """
        logging.info("Migrating VM %s from %s to %s" %
                     (self.name, self.connect_uri, dest_uri))
        result = virsh.migrate(self.name, dest_uri, option,
                               extra, uri=self.connect_uri,
                               ignore_status=ignore_status,
                               debug=debug)
        # On successful migration, point to guests new hypervisor.
        # Since dest_uri could be None, checking it is necessary.
        if result.exit_status == 0 and dest_uri:
            self.connect_uri = dest_uri
        return result


    def attach_device(self, xml_file, extra=""):
        """
        Attach a device to VM.
        """
        return virsh.attach_device(self.name, xml_file, extra,
                                   uri=self.connect_uri)


    def detach_device(self, xml_file, extra=""):
        """
        Detach a device from VM.
        """
        return virsh.detach_device(self.name, xml_file, extra,
                                   uri=self.connect_uri)


    def attach_interface(self, option="", ignore_status=False,
                         debug=False):
        """
        Attach a NIC to VM.
        """
        return virsh.attach_interface(self.name, option,
                                      uri=self.connect_uri,
                                      ignore_status=ignore_status,
                                      debug=debug)


    def detach_interface(self, option="", ignore_status=False,
                         debug=False):
        """
        Detach a NIC to VM.
        """
        return virsh.detach_interface(self.name, option,
                                      uri=self.connect_uri,
                                      ignore_status=ignore_status,
                                      debug=debug)


    def destroy(self, gracefully=True, free_mac_addresses=True):
        """
        Destroy the VM.

        If gracefully is True, first attempt to shutdown the VM with a shell
        command. If that fails, send SIGKILL to the qemu process.

        @param gracefully: If True, an attempt will be made to end the VM
                using a shell command before trying to end the qemu process
                with a 'quit' or a kill signal.
        @param free_mac_addresses: If vm is undefined with libvirt, also
                                   release/reset associated mac address
        """
        try:
            # Is it already dead?
            if self.is_alive():
                logging.debug("Destroying VM")
                if gracefully and self.params.get("shutdown_command"):
                    # Try to destroy with shell command
                    logging.debug("Trying to shutdown VM with shell command")
                    try:
                        session = self.login()
                    except (remote.LoginError, virt_vm.VMError), e:
                        logging.debug(e)
                    else:
                        try:
                            # Send the shutdown command
                            session.sendline(self.params.get("shutdown_command"))
                            logging.debug("Shutdown command sent; waiting for VM "
                                          "to go down...")
                            if utils_misc.wait_for(self.is_dead, 60, 1, 1):
                                logging.debug("VM is down")
                                return
                        finally:
                            session.close()
                virsh.destroy(self.name, uri=self.connect_uri)

        finally:
            if self.serial_console:
                self.serial_console.close()
            for f in ([self.get_testlog_filename(),
                       self.get_serial_console_filename()]):
                try:
                    os.unlink(f)
                except OSError:
                    pass
            if hasattr(self, "migration_file"):
                try:
                    os.unlink(self.migration_file)
                except OSError:
                    pass

        if free_mac_addresses:
            if self.is_persistent():
                logging.warning("Requested MAC address release from "
                                "persistent vm %s. Ignoring." % self.name)
            else:
                logging.debug("Releasing MAC addresses for vm %s." % self.name)
                for nic_name in self.virtnet.nic_name_list():
                    self.virtnet.free_mac_address(nic_name)


    def remove(self):
        self.destroy(gracefully=True, free_mac_addresses=False)
        if not self.undefine():
            raise virt_vm.VMRemoveError("VM '%s' undefine error" % self.name)
        self.destroy(gracefully=False, free_mac_addresses=True)
        logging.debug("VM '%s' was removed", self.name)


    def get_uuid(self):
        """
        Return VM's UUID.
        """
        uuid = virsh.domuuid(self.name, uri=self.connect_uri)
        # only overwrite it if it's not set
        if self.uuid is None:
            self.uuid = uuid
        return self.uuid


    def get_ifname(self, nic_index=0):
        raise NotImplementedError


    def get_virsh_mac_address(self, nic_index=0):
        """
        Get the MAC of this VM domain.

        @param nic_index: Index of the NIC
        @raise VMMACAddressMissingError: If no MAC address is defined for the
                requested NIC
        """
        thexml = virsh.dumpxml(self.name, uri=self.connect_uri)
        xtf = xml_utils.XMLTreeFile(thexml)
        interfaces = xtf.find('devices').findall('interface')
        # Range check
        try:
            mac = interfaces[nic_index].find('mac').get('address')
            if mac is not None:
                return mac
        except IndexError:
            pass # Allow other exceptions through
        # IndexError (range check) or mac == None
        raise virt_vm.VMMACAddressMissingError(nic_index)


    def get_pid(self):
        """
        Return the VM's PID.

        @return: int with PID. If VM is not alive, returns None.
        """
        pid_file = "/var/run/libvirt/qemu/%s.pid" % self.name
        pid = None
        if os.path.exists(pid_file):
            try:
                pid_file_contents = open(pid_file).read()
                pid = int(pid_file_contents)
            except IOError:
                logging.error("Could not read %s to get PID", pid_file)
            except TypeError:
                logging.error("PID file %s has invalid contents: '%s'",
                              pid_file, pid_file_contents)
        else:
            logging.debug("PID file %s not present", pid_file)

        return pid


    def get_vcpus_pid(self):
        """
        Return the vcpu's pid for a given VM.

        @return: list of PID of vcpus of a VM.
        """
        output = virsh.qemu_monitor_command(self.name, "info cpus",
                                            uri=self.connect_uri)
        vcpu_pids = re.findall(r'thread_id=(\d+)', output.stdout)
        return vcpu_pids


    def get_shell_pid(self):
        """
        Return the PID of the parent shell process.

        @note: This works under the assumption that self.process.get_pid()
        returns the PID of the parent shell process.
        """
        return self.process.get_pid()


    def get_shared_meminfo(self):
        """
        Returns the VM's shared memory information.

        @return: Shared memory used by VM (MB)
        """
        if self.is_dead():
            logging.error("Could not get shared memory info from dead VM.")
            return None

        filename = "/proc/%d/statm" % self.get_pid()
        shm = int(open(filename).read().split()[2])
        # statm stores informations in pages, translate it to MB
        return shm * 4.0 / 1024

    def activate_nic(self, nic_index_or_name):
        #TODO: Impliment nic hotplugging
        pass # Just a stub for now

    def deactivate_nic(self, nic_index_or_name):
        #TODO: Impliment nic hot un-plugging
        pass # Just a stub for now

    @error.context_aware
    def reboot(self, session=None, method="shell", nic_index=0, timeout=240):
        """
        Reboot the VM and wait for it to come back up by trying to log in until
        timeout expires.

        @param session: A shell session object or None.
        @param method: Reboot method.  Can be "shell" (send a shell reboot
                command).
        @param nic_index: Index of NIC to access in the VM, when logging in
                after rebooting.
        @param timeout: Time to wait for login to succeed (after rebooting).
        @return: A new shell session object.
        """
        error.base_context("rebooting '%s'" % self.name, logging.info)
        error.context("before reboot")
        session = session or self.login()
        error.context()

        if method == "shell":
            session.sendline(self.params.get("reboot_command"))
        else:
            raise virt_vm.VMRebootError("Unknown reboot method: %s" % method)

        error.context("waiting for guest to go down", logging.info)
        if not utils_misc.wait_for(lambda:
                                  not session.is_responsive(timeout=30),
                                  120, 0, 1):
            raise virt_vm.VMRebootError("Guest refuses to go down")
        session.close()

        error.context("logging in after reboot", logging.info)
        return self.wait_for_login(nic_index, timeout=timeout)


    def screendump(self, filename, debug=False):
        if debug:
            logging.debug("Requesting screenshot %s" % filename)
        return virsh.screenshot(self.name, filename, uri=self.connect_uri)


    def start(self):
        """
        Starts this VM.
        """
        self.uuid = virsh.domuuid(self.name, uri=self.connect_uri)
        # Pull in mac addresses from libvirt guest definition
        for index, nic in enumerate(self.virtnet):
            try:
                mac = self.get_virsh_mac_address(index)
                if not nic.has_key('mac'):
                    logging.debug("Updating nic %d with mac %s on vm %s"
                                  % (index, mac, self.name))
                    nic.mac = mac
                elif nic.mac != mac:
                    logging.warning("Requested mac %s doesn't match mac %s "
                                    "as defined for vm %s" % (nic.mac, mac,
                                    self.name))
                #TODO: Checkout/Set nic_model, nettype, netdst also
            except virt_vm.VMMACAddressMissingError:
                logging.warning("Nic %d requested by test but not defined for"
                                " vm %s" % (index, self.name))
        if virsh.start(self.name, uri=self.connect_uri):
            # Wait for the domain to be created
            has_started = utils_misc.wait_for(func=self.is_alive, timeout=60,
                                              text=("waiting for domain %s "
                                                    "to start" % self.name))
            if has_started is None:
                raise virt_vm.VMStartError(self.name, "libvirt domain not "
                                                      "active after start")
            self.uuid = virsh.domuuid(self.name, uri=self.connect_uri)
        else:
            raise virt_vm.VMStartError(self.name, "libvirt domain failed "
                                                  "to start")


    def wait_for_shutdown(self, count=60):
        """
        Return True on successful domain shutdown.

        Wait for a domain to shutdown, libvirt does not block on domain
        shutdown so we need to watch for successful completion.

        @param name: VM name
        @param name: Optional timeout value
        """
        timeout = count
        while count > 0:
            # check every 5 seconds
            if count % 5 == 0:
                if virsh.is_dead(self.name, uri=self.connect_uri):
                    logging.debug("Shutdown took %d seconds", timeout - count)
                    return True
            count -= 1
            time.sleep(1)
            logging.debug("Waiting for guest to shutdown %d", count)
        return False


    def shutdown(self):
        """
        Shuts down this VM.
        """
        try:
            if virsh.domstate(self.name) != 'shut off':
                virsh.shutdown(self.name, uri=self.connect_uri)
            if self.wait_for_shutdown():
                logging.debug("VM %s shut down", self.name)
                return True
            else:
                logging.error("VM %s failed to shut down", self.name)
                return False
        except error.CmdError:
            logging.error("VM %s failed to shut down", self.name)
            return False


    def pause(self):
        return virsh.suspend(self.name, uri=self.connect_uri)


    def resume(self):
        return virsh.resume(self.name, uri=self.connect_uri)


    def save_to_file(self, path):
        """
        Override BaseVM save_to_file method
        """
        state = virsh.domstate(self.name)
        if state not in ('paused',):
            raise virt_vm.VMStatusError("Cannot save a VM that is %s" % state)
        logging.debug("Saving VM %s to %s" %(self.name, path))
        virsh.save(self.name, path, uri=self.connect_uri)
        state = virsh.domstate(self.name)
        if state not in ('shut off',):
            raise virt_vm.VMStatusError("VM not shut off after save")


    def restore_from_file(self, path):
        """
        Override BaseVM restore_from_file method
        """
        state = virsh.domstate(self.name)
        if state not in ('shut off',):
            raise virt_vm.VMStatusError("Can not restore VM that is %s" % state)
        logging.debug("Restoring VM from %s" % path)
        virsh.restore(path, uri=self.connect_uri)
        state = virsh.domstate(self.name)
        if state not in ('paused','running'):
            raise virt_vm.VMStatusError("VM not paused after restore, it is %s." %
                   state)


    def vcpupin(self, vcpu, cpu):
        """
        To pin vcpu to cpu
        """
        virsh.vcpupin(self.name, vcpu, cpu, uri=self.connect_uri)


    def dominfo(self):
        """
        Return a dict include vm's infomation.
        """
        output = virsh.dominfo(self.name, uri=self.connect_uri)
        # Key: word before ':' | value: content after ':' (stripped)
        dominfo_dict = {}
        for line in output.splitlines():
            key = line.split(':')[0].strip()
            value = line.split(':')[-1].strip()
            dominfo_dict[key] = value
        return dominfo_dict


    def vcpuinfo(self):
        """
        Return a dict's list include vm's vcpu infomation.
        """
        output = virsh.vcpuinfo(self.name, uri=self.connect_uri)
        # Key: word before ':' | value: content after ':' (stripped)
        vcpuinfo_list = []
        vcpuinfo_dict = {}
        for line in output.splitlines():
            key = line.split(':')[0].strip()
            value = line.split(':')[-1].strip()
            vcpuinfo_dict[key] = value
            if key == "CPU Affinity":
                vcpuinfo_list.append(vcpuinfo_dict)
        return vcpuinfo_list


    def get_used_mem(self):
        """
        Get vm's current memory(kilobytes).
        """
        dominfo_dict = self.dominfo()
        memory = dominfo_dict['Used memory'].split(' ')[0] # strip off ' kb'
        return int(memory)


    def get_blk_devices(self):
        """
        Get vm's block devices.

        Return a dict include all devices detail info.
        example:
        {target: {'type': value, 'device': value, 'source': value}}
        """
        domblkdict = {}
        options = "--details"
        result = virsh.domblklist(self.name, options, ignore_status=True,
                                  uri=self.connect_uri)
        blklist = result.stdout.strip().splitlines()
        if result.exit_status != 0:
            logging.info("Get vm devices failed.")
        else:
            blklist = blklist[2:]
            for line in blklist:
                linesplit = line.split(None, 4)
                target = linesplit[2]
                blk_detail = {'type': linesplit[0],
                              'device': linesplit[1],
                              'source': linesplit[3]}
                domblkdict[target] = blk_detail
        return domblkdict


    def get_disk_devices(self):
        """
        Get vm's disk type block devices.
        """
        blk_devices = self.get_blk_devices()
        disk_devices = {}
        for target in blk_devices:
            details = blk_devices[target]
            if details['device'] == "disk":
                disk_devices[target] = details
        return disk_devices
