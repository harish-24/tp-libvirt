import os
import random
import logging

from autotest.client import utils
from autotest.client.shared import error

from avocado.utils import path as utils_path

from virttest.utils_test import libvirt as utlv
from virttest import libvirt_storage
from virttest import virsh
from virttest import utils_misc

from provider import libvirt_version


def run(test, params, env):
    """
    This test cover two volume commands: vol-clone and vol-wipe.

    1. Create a given type pool.
    2. Create a given format volume in the pool.
    3. Clone the new create volume.
    4. Wipe the new clone volume.
    5. Delete the volume and pool.
    """

    pool_name = params.get("pool_name")
    pool_type = params.get("pool_type")
    pool_target = params.get("pool_target")
    if not os.path.dirname(pool_target):
        pool_target = os.path.join(test.tmpdir, pool_target)
    emulated_image = params.get("emulated_image")
    emulated_image_size = params.get("emulated_image_size")
    vol_name = params.get("vol_name")
    new_vol_name = params.get("new_vol_name")
    vol_capability = params.get("vol_capability")
    vol_format = params.get("vol_format")
    clone_option = params.get("clone_option", "")
    wipe_algorithms = params.get("wipe_algorithms")

    if virsh.has_command_help_match("vol-clone", "--prealloc-metadata") is None:
        if "prealloc-metadata" in clone_option:
            raise error.TestNAError("Option --prealloc-metadata "
                                    "is not supported.")

    clone_status_error = "yes" == params.get("clone_status_error", "no")
    wipe_status_error = "yes" == params.get("wipe_status_error", "no")
    setup_libvirt_polkit = "yes" == params.get("setup_libvirt_polkit")

    # libvirt acl polkit related params
    uri = params.get("virsh_uri")
    unpri_user = params.get('unprivileged_user')
    if unpri_user:
        if unpri_user.count('EXAMPLE'):
            unpri_user = 'testacl'

    if not libvirt_version.version_compare(1, 1, 1):
        if setup_libvirt_polkit:
            raise error.TestNAError("API acl test not supported in current"
                                    " libvirt version.")

    # Using algorithms other than zero need scrub installed.
    try:
        utils_path.find_command('scrub')
    except utils_path.CmdNotFoundError:
        logging.warning("Can't locate scrub binary, only 'zero' algorithm "
                        "is used.")
        valid_algorithms = ["zero"]
    else:
        valid_algorithms = ["zero", "nnsa", "dod", "bsi", "gutmann",
                            "schneier", "pfitzner7", "pfitzner33", "random"]

    # Choose an algorithm randomly
    if wipe_algorithms:
        alg = random.choice(wipe_algorithms.split())
    else:
        alg = random.choice(valid_algorithms)

    libvirt_pvt = utlv.PoolVolumeTest(test, params)
    libvirt_pool = libvirt_storage.StoragePool()
    if libvirt_pool.pool_exists(pool_name):
        raise error.TestError("Pool '%s' already exist" % pool_name)
    try:
        # Create a new pool
        disk_vol = []
        if pool_type == 'disk':
            disk_vol.append(params.get("pre_vol", '10M'))
        libvirt_pvt.pre_pool(pool_name=pool_name,
                             pool_type=pool_type,
                             pool_target=pool_target,
                             emulated_image=emulated_image,
                             image_size=emulated_image_size,
                             pre_disk_vol=disk_vol)

        libvirt_vol = libvirt_storage.PoolVolume(pool_name)
        # Create a new volume
        if vol_format in ['raw', 'qcow2', 'qed', 'vmdk']:
            libvirt_pvt.pre_vol(vol_name=vol_name,
                                vol_format=vol_format,
                                capacity=vol_capability,
                                allocation=None,
                                pool_name=pool_name)
        elif vol_format == 'partition':
            vol_name = utlv.get_vol_list(pool_name).keys()[0]
            logging.debug("Find partition %s in disk pool", vol_name)
        elif vol_format == 'sparse':
            # Create a sparse file in pool
            sparse_file = pool_target + '/' + vol_name
            cmd = "dd if=/dev/zero of=" + sparse_file
            cmd += " bs=1 count=0 seek=" + vol_capability
            utils.run(cmd)
        else:
            raise error.TestError("Unknown volume format %s" % vol_format)

        # Refresh the pool
        virsh.pool_refresh(pool_name, debug=True)
        vol_info = libvirt_vol.volume_info(vol_name)
        if not vol_info:
            raise error.TestError("Fail to get info of volume %s" % vol_name)

        for key in vol_info:
            logging.debug("Original volume info: %s = %s", key, vol_info[key])

        # Metadata preallocation is not support for block volume
        if vol_info["Type"] == "block" and clone_option.count("prealloc-metadata"):
            clone_status_error = True

        if pool_type == "disk":
            new_vol_name = utlv.new_disk_vol_name(pool_name)
            if new_vol_name is None:
                raise error.TestError("Fail to generate volume name")
            # update polkit rule as the volume name changed
            if setup_libvirt_polkit:
                vol_pat = r"lookup\('vol_name'\) == ('\S+')"
                new_value = "lookup('vol_name') == '%s'" % new_vol_name
                utlv.update_polkit_rule(params, vol_pat, new_value)

        # Clone volume
        clone_result = virsh.vol_clone(vol_name, new_vol_name, pool_name,
                                       clone_option, debug=True)
        if not clone_status_error:
            if clone_result.exit_status != 0:
                raise error.TestFail("Clone volume fail:\n%s" %
                                     clone_result.stderr.strip())
            else:
                vol_info = libvirt_vol.volume_info(new_vol_name)
                for key in vol_info:
                    logging.debug("Cloned volume info: %s = %s", key,
                                  vol_info[key])
                logging.debug("Clone volume successfully.")
                # Wipe the new clone volume
                if alg:
                    logging.debug("Wiping volume by '%s' algorithm", alg)
                wipe_result = virsh.vol_wipe(new_vol_name, pool_name, alg,
                                             unprivileged_user=unpri_user,
                                             uri=uri, debug=True)
                unsupported_err = ["Unsupported algorithm",
                                   "no such pattern sequence"]
                if not wipe_status_error:
                    if wipe_result.exit_status != 0:
                        if any(err in wipe_result.stderr for err in unsupported_err):
                            raise error.TestNAError(wipe_result.stderr)
                        raise error.TestFail("Wipe volume fail:\n%s" %
                                             clone_result.stdout.strip())
                    else:
                        virsh_vol_info = libvirt_vol.volume_info(new_vol_name)
                        for key in virsh_vol_info:
                            logging.debug("Wiped volume info(virsh): %s = %s",
                                          key, virsh_vol_info[key])
                        vol_path = virsh.vol_path(new_vol_name,
                                                  pool_name).stdout.strip()
                        qemu_vol_info = utils_misc.get_image_info(vol_path)
                        for key in qemu_vol_info:
                            logging.debug("Wiped volume info(qemu): %s = %s",
                                          key, qemu_vol_info[key])
                            if qemu_vol_info['format'] != 'raw':
                                raise error.TestFail("Expect wiped volume "
                                                     "format is raw")
                elif wipe_status_error and wipe_result.exit_status == 0:
                    raise error.TestFail("Expect wipe volume fail, but run"
                                         " successfully.")
        elif clone_status_error and clone_result.exit_status == 0:
            raise error.TestFail("Expect clone volume fail, but run"
                                 " successfully.")
    finally:
        # Clean up
        try:
            libvirt_pvt.cleanup_pool(pool_name, pool_type, pool_target,
                                     emulated_image)
        except error.TestFail, detail:
            logging.error(str(detail))
