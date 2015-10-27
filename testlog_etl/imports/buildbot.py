# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Kyle Lahnakoski (kyle@lahnakoski.com)
#

from __future__ import unicode_literals
from __future__ import division

import re

from pyLibrary import convert, strings
from pyLibrary.debugs.logs import Log
from pyLibrary.dot import wrap, Dict, coalesce, set_default, unwraplist
from pyLibrary.maths import Math
from pyLibrary.times.dates import Date, unicode2datetime


BUILDBOT_LOGS = "http://builddata.pub.build.mozilla.org/builddata/buildjson/"

class BuildbotTranslator(object):

    def __init__(self):
        self.unknown_platforms=[]

    def parse(self, data):
        data = wrap(data)
        output = Dict()

        output.action.reason = data.reason
        output.action.request_time = data.requesttime
        output.action.start_time = data.starttime
        output.action.end_time = data.endtime
        output.action.buildbot_status = STATUS_CODES[data.result]

        props = data.properties
        if not props or not props.buildername:
            output.other = props
            return output

        output.action.job_number = props.buildnumber
        for k, v in props.request_times.items():
            output.action.requests += [{"request_id": int(k), "timestamp": v}]

        output.run.key = key = props.buildername
        if key.startswith("TB "):
            key = key[3:]

        try:
            ratio = RATIO_PATTERN.match(key.split("_")[-1])
            if ratio:
                output.action.step = ratio.groups()[0]
        except Exception, e:
            Log.error("problem in\n{{data|json}}", data=data, cause=e)

        # SCRIPT
        output.run.script.url = props.script_repo_url
        output.run.script.revision = props.script_repo_revision

        # REVISIONS
        output.build.revision = coalesce(props.revision, props.gecko_revision)
        output.build.revision12 = props.revision[0:12]
        if props.gecko_revision:
            if props.gecko_revision[0:12] != output.build.revision12:
                Log.error("expecting revision to be the gecko revision")
            output.build.gecko_revision = output.build.revision
            output.build.gecko_revision12 = output.build.revision[0:12]
            output.build.gaia_revision = props.gaia_revision
            output.build.gaia_revision12 = props.gaia_revision[0:12]

        output.version = props.version

        # BUILD ID AND DATE
        try:
            output.build.date = Date(unicode2datetime(props.buildid, "%Y%m%d%H%M%S"))
            output.build.id = props.buildid
            props.buildid = None
        except Exception, _:
            output.build.id = "<error>"

        # LOCALE
        output.build.locale = coalesce(props.locale, 'en-US')
        if props.locales:  # nightly repack build
            output.action.repack = True
            data.build.locale = None
            try:
                data.build.locales = convert.json2value(props.locales).keys()
            except Exception:
                data.build.locales = props.locales.split(",")

        output.build.url = coalesce(props.packageUrl, props.build_url, props.fileURL)
        output.run.logurl = props.log_url
        output.build.release = coalesce(props.en_revision, props.script_repo_revision)
        output.run.machine.name = coalesce(props.slavename, props.aws_instance_id)
        output.run.machine.aws_id = props.aws_instance_id
        split_name = output.run.machine.name.split("-")
        if Math.is_integer(split_name[-1]):
            # EXAMPLES
            # b-2008-ix-0106
            # t-w732-ix-047
            # bld-linux64-spot-013
            # panda-0150
            output.run.machine.pool = "-".join(split_name[:-1])
        output.run.machine.aws_type = props.aws_instance_type

        # FILES
        try:
            if props.blobber_files:
                files = convert.json2value(props.blobber_files)
                output.run.files = [
                    {"name": name, "url": url}
                    for name, url in files.items()
                ]
        except Exception, e:
            Log.error("Malformed `blobber_files` buildbot property: {{json}}", json=props.blobber_files, cause=e)

        # PRODUCT
        output.build.product = props.product.lower()
        if "xulrunner" in key:
            output.build.product = "xulrunner"

        # PLATFORM
        raw_platform = props.platform
        if raw_platform:
            if "Code Coverage " in key:
                if raw_platform.endswith("-cc"):
                    raw_platform = raw_platform[:-3]
                else:
                    Log.error("Not recognized: {{key}}\n{{data|json}}", key=key, data=data)
                key = key.replace("Code Coverage ", "")
                output.tags += ["code coverage"]

            if raw_platform not in KNOWN_PLATFORM:
                KNOWN_PLATFORM[raw_platform] = {"build": {"platform": "unknown"}}
                Log.error("Unknown platform {{platform}}\n{{data|json}}", platform=raw_platform, data=data)
            set_default(output, KNOWN_PLATFORM[raw_platform])

        # BRANCH
        output.build.branch = branch_name = props.branch.split("/")[-1]
        if not branch_name:
            Log.error("{{key|quote}} no 'branch' property", key=key)

        if 'release' in key:
            output.tags += ['release']
        if key.endswith("nightly"):
            output.tags += ["nightly"]

        # DECODE buildername
        for b in BUILDER_NAMES:
            expected = strings.expand_template(b, {
                "branch": branch_name,
                "platform": raw_platform,
                "clean_platform": output.build.platform,
                "product": output.build.product,
                "vm": output.run.machine.vm,
                "step": output.action.step,
            })
            if key == expected:
                output.build.name = props.buildername
                scrub_known_properties(props)
                output.other = props
                output.action.build = True
                verify(output, data)
                return output

        if key.startswith("fuzzer"):
            output.build.product = "fuzzing"
            pass
        elif 'l10n' in key or 'repack' in key:
            output.action.repack = True
        elif key.startswith("jetpack-"):
            for t in BUILD_TYPES:
                if key.endswith("-" + t):
                    output.build.type += [t]

            match = re.match(strings.expand_template(
                "jetpack-(.*)-{{platform}}-{{type}}",
                {
                    "platform": raw_platform,
                    "type": unwraplist(output.build.type)
                }
            ), key)

            if not match:
                Log.error("Not recognized: {{key}} in \n{{data|json}}", key=key, data=data)

            if branch_name == "addon-sdk":
                output.build.branch = match.groups()[0]
        elif key.endswith("nightly"):
            try:
                output.build.name = props.buildername
                raw_platform, build = key.split(" " + branch_name + " ")
                set_default(output, TEST_PLATFORMS[raw_platform])

                for t in BUILD_TYPES:
                    if t in build:
                        output.build.type += [t]
            except Exception:
                Log.error("Not recognized: {{key}} in \n{{data|json}}", key=key, data=data)

        elif key.endswith("build"):
            try:
                raw_platform, build = key.split(" " + branch_name + " ")
                output.build.name = raw_platform
                if raw_platform not in TEST_PLATFORMS:
                    if raw_platform not in self.unknown_platforms:
                        self.unknown_platforms += [raw_platform]
                        Log.error("Platform not recognized: {{platform}}\n{{data}}", platform=raw_platform, data=data)
                    else:
                        return Dict()  # ERROR INGNORED, ALREADY SENT
                set_default(output, TEST_PLATFORMS[raw_platform])
                output.action.build = True
            except Exception, e:
                raise Log.error("Not recognized: {{key}}\n{{data|json}}", key=key, data=data)

            for t in BUILD_FEATURES:
                if t in build:
                    output.tags += [t]
            for t in BUILD_TYPES:
                if t in build:
                    output.build.type += [t]
        elif key.endswith("valgrind"):
            output.build.name = props.buildername
            raw_platform, build = key.split(" " + branch_name + " ")
            set_default(output, TEST_PLATFORMS[raw_platform])
        else:
            # FORMAT: <platform> <branch> <test_mode> <test_name> <other>
            try:
                raw_platform, test = key.split(" " + branch_name + " ")
            except Exception:
                Log.error("Not recognized: {{key}}\n{{data}}", key=key, data=data)

            output.build.name = raw_platform
            if raw_platform not in TEST_PLATFORMS:
                Log.error("Platform not recognized: {{platform}}\n{{data}}", platform=raw_platform, data=data)
                if raw_platform not in self.unknown_platforms:
                    self.unknown_platforms += [raw_platform]
                    Log.error("Platform not recognized: {{platform}}\n{{data}}", platform=raw_platform, data=data)
                else:
                    return Dict()  # ERROR INGNORED, ALREADY SENT

            set_default(output, TEST_PLATFORMS[raw_platform])

            parsed = parse_test(test, output)
            if not parsed:
                Log.error("Test mode not recognized: {{key}}\n{{data|json}}", key=key, data=data)

        output.other = props
        verify(output, data)
        return output


def verify(output, data):
    if "e10s" in data.properties.buildername.lower() and output.run.type != 'e10s':
        Log.error("Did not pickup e10s in\n{{data|json}}", data=data)
    if output.run.machine.os != None and output.run.machine.os not in ALLOWED_OS:
        ALLOWED_OS.append(output.run.machine.os)
        Log.error("Bad OS {{os}}\n{{data|json}}", os=output.run.machine.os, data=data)
    if output.action.test and output.build.platform not in ALLOWED_PLATFORMS:
        ALLOWED_PLATFORMS.append(output.build.platform)
        Log.error("Bad Platform {{platform}}\n{{data|json}}", platform=output.build.platform, data=data)
    if output.build.product not in ALLOWED_PRODUCTS:
        ALLOWED_PRODUCTS.append(output.build.product)
        Log.error("Bad Product {{product}}\n{{data|json}}", product=output.build.product, data=data)

    output.build.tags = set(output.build.tags)
    output.build.type = set(output.build.type)
    scrub_known_properties(data.properties)


def parse_test(test, output):
    # "web-platform-tests-e10s-7"
    test = test.lower()

    # CHUNK NUMBER
    path = test.split("-")
    if Math.is_integer(path[-1]):
        output.run.chunk = int(path[-1])
        test = "-".join(path[:-1])

    if "-e10s" in test:
        test = test.replace("-e10s", "")
        output.run.type = "e10s"

    for m, d in test_modes.items():
        if test.startswith(m):
            set_default(output, d)
            output.run.suite = test[len(m):].strip()
            return True

    return False

def scrub_known_properties(props):
    props.aws_instance_id = None
    props.aws_instance_type = None
    props.blobber_files = None
    props.branch = None
    props.buildername = None
    # props.buildid = None   # SOMETIMES THIS IS BADLY FORMATTED, KEEP IT
    props.buildnumber = None
    props.build_url = None
    props.commit_titles = None  # DO NOT STORE
    props.fileURL = None
    props.en_revision = None,
    props.gecko_revision = None
    props.gaia_revision = None
    props.locale = None
    props.locales = None
    props.log_url = None
    props.packageUrl = None
    props.platform = None
    props.product = None
    props.request_ids = None
    props.request_times = None
    props.revision = None
    props.repo_path = None
    props.script_repo_revision = None
    props.script_repo_url = None
    props.slavename = None
    props.version = None



test_modes = {
    "debug test": {"build": {"type": ["debug"]}, "action": {"test": True}},
    "opt test": {"build": {"type": ["opt"]}, "action": {"test": True}},
    "pgo test": {"build": {"type": ["pgo"]}, "action": {"test": True}},
    "pgo talos": {"build": {"type": ["pgo"]}, "action": {"test": True, "talos": True}},
    "talos": {"action": {"test": True, "talos": True}}
}

BUILDER_NAMES = [
    'b2g_{{branch}}_{{platform}} build',
    'b2g_{{branch}}_{{platform}}-debug_periodic',
    'b2g_{{branch}}_{{platform}}_dep',
    'b2g_{{branch}}_{{platform}}_nightly',
    'b2g_{{branch}}_{{platform}} nightly',
    'b2g_{{branch}}_{{platform}}_periodic',
    'b2g_{{branch}}_emulator-debug_dep',
    'b2g_{{branch}}_emulator_dep',
    'b2g_{{branch}}_{{product}}_eng_periodic', # {"build":{"product":"{{product}}"}}
    '{{branch}}-{{product}}_{{platform}}_build',
    '{{branch}}-{{product}}_antivirus',
    '{{branch}}-{{product}}_almost_ready_for_release',
    '{{branch}}-{{product}}_beta_ready_for_beta-cdntest_testing',
    '{{branch}}-{{product}}_beta_ready_for_release',
    '{{branch}}-{{product}}_beta_start_uptake_monitoring',
    '{{branch}}-{{product}}_beta_updates',
    '{{branch}}-{{product}}_bouncer_submitter',
    '{{branch}}-{{product}}_checksums',
    '{{branch}}-{{product}}-esr_final_verification',
    '{{branch}}-{{product}}-esr_ready-for-esr-cdntest',
    '{{branch}}-{{product}}_esr_ready_for_esr-cdntest_testing',
    '{{branch}}-{{product}}_esr_ready_for_release',
    '{{branch}}-{{product}}_esr_start_uptake_monitoring',
    '{{branch}}-{{product}}_esr_updates',
    '{{branch}}-{{product}}_push_to_mirrors',
    '{{branch}}-{{product}}_postrelease',
    '{{branch}}-{{product}}_ready_for_releasetest_testing',
    '{{branch}}-{{product}}_reset_schedulers',
    '{{branch}}-{{product}}_release_ready_for_release-cdntest_testing',
    '{{branch}}-{{product}}_release_ready_for_release',
    '{{branch}}-{{product}}_release_start_uptake_monitoring',
    '{{branch}}-{{product}}_release_updates',
    '{{branch}}-{{product}}_source',
    '{{branch}}-{{product}}_start_uptake_monitoring',
    '{{branch}}-{{product}}_tag_source',
    '{{branch}}-{{product}}_updates',

    '{{branch}}-{{platform}}_build',
    '{{branch}}-{{platform}}_update_verify_{{step}}',
    '{{branch}}-{{platform}}_update_verify_beta_{{step}}',
    '{{branch}}-{{platform}}_update_verify_esr_{{step}}',
    '{{branch}}-{{platform}}_update_verify_release_{{step}}',
    '{{branch}}-{{platform}}_ui_update_verify_beta_{{step}}',

    '{{branch}}-antivirus',
    '{{branch}}-beta_final_verification',
    '{{branch}}-check_permissions',
    '{{branch}}-esr_final_verification',
    '{{branch}}-final_verification',
    '{{branch}} hg bundle',
    '{{branch}}-release_final_verification',
    '{{branch}}-update_shipping',
    '{{branch}}-update_shipping_beta',
    '{{branch}}-update_shipping_esr',
    '{{branch}}-update_shipping_release',
    '{{branch}}-xr_postrelease',
    '{{platform}}_{{branch}}_dep',
    '{{platform}} {{branch}} periodic file update',
    'Linux x86-64 {{branch}} periodic file update',  # THE platform DOES NOT MATCH
    '{{vm}}_{{branch}}_{{clean_platform}} nightly',
    '{{vm}}_{{branch}}_{{clean_platform}} build'
]

BUILD_TYPES = [
    "asan",   # ADDRESS SANITIZER
    "debug",  # FOR DEBUGGING
    "mulet",  # COMMON FRAMEWORK FOR b2g and Firefox
    "opt",    # OPTIMIZED
    "pgo",    # PROFILE GUIDED OPTIMIZATIONS
    "tsan"    # THREAD SANITIZER
]

BUILD_FEATURES = [
    "leak test",
    "static analysis"
]

TEST_PLATFORMS = {
    "Android 2.3": {"run": {"machine": {"os": "android 2.3"}}, "build": {"platform": "android"}},
    "Android 2.3 Armv6 Emulator": {"run": {"machine": {"os": "android 2.3", "type": "emulator armv6"}}, "build": {"platform": "android"}},
    "Android 2.3 Emulator": {"run": {"machine": {"os": "android 2.3", "type": "emulator"}}, "build": {"platform": "android"}},
    "Android 2.3 Debug": {"run": {"machine": {"os": "android 2.3"}}, "build": {"platform": "android", "type": ["debug"]}},
    "Android 4.0 armv7 API 11+": {"run": {"machine": {"os": "android 4.0", "type": "arm7"}}, "build": {"platform": "andriod"}},
    "Android 4.0 Panda": {"run": {"machine": {"os": "android 4.0", "type": "panda"}}, "build": {"platform": "android"}},
    "Android 4.2 x86": {"run": {"machine": {"os": "android 4.2", "type": "emulator x86"}}, "build": {"platform": "android"}},
    "Android 4.2 x86 Emulator": {"run": {"machine": {"os": "android 4.2", "type": "emulator x86"}}, "build": {"platform": "android"}},
    "Android 4.3 armv7 API 11+": {"run": {"machine": {"os": "android 4.3", "type": "arm7"}}, "build": {"platform": "android"}},
    "Android armv7 API 11+": {"run": {"machine": {"os": "android 3.0", "type": "arm7"}}, "build": {"platform": "android"}},
    "Android armv7 API 9": {"run": {"machine": {"os": "android 2.3", "type": "arm7"}}, "build": {"platform": "android"}},
    "b2g_b2g-inbound_emulator_dep": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_ubuntu64_vm": {"run": {"machine": {"os": "b2g", "type": "emulator64"}}, "build": {"platform": "b2g", "product": "b2g"}},
    "b2g_emulator-kk_vm":{"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "flame", "product": "b2g"}},
    "b2g_emulator_vm": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g", "product": "b2g"}},
    "b2g_emulator_vm_large": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_emulator-jb_vm": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_macosx64": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_mozilla-central_emulator_nightly": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_mozilla-central_flame-kk_nightly": {"run": {"machine": {"os": "b2g", "type": "flame"}}, "build": {"platform": "b2g"}},
    "b2g_mozilla-inbound_emulator_dep": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_mozilla-inbound_emulator-debug_dep": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g", "type": ["debug"]}},
    "b2g_try_emulator_dep": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "b2g_try_emulator-debug_dep": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g", "type": ["debug"]}},
    "b2g_ubuntu32_vm": {"run": {"machine": {"os": "b2g", "type": "emulator32"}}, "build": {"platform": "b2g"}},
    "Linux": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux32"}},
    "Linux x86-64": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux64"}},
    "Linux x86-64 Mulet": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux64", "type": ["mulet"]}},
    "OS X 10.7": {"run": {"machine": {"os": "lion 10.7"}}, "build": {"platform": "macosx64"}},
    "OS X 10.7 64-bit": {"run": {"machine": {"os": "lion 10.7"}}, "build": {"platform": "macosx64"}},
    "OS X Mulet": {"run": {"machine": {"os": "macosx"}}, "build": {"platform": "macosx", "type": ["mulet"]}},
    "Rev5 MacOSX Yosemite 10.10": {"run": {"machine": {"os": "yosemite 10.10"}}, "build": {"platform": "macosx64"}},
    "Rev5 MacOSX Yosemite 10.10.5": {"run": {"machine": {"os": "yosemite 10.10"}}, "build": {"platform": "macosx64"}},
    "Rev4 MacOSX Snow Leopard 10.6": {"run": {"machine": {"os": "snowleopard 10.6"}}, "build": {"platform": "macosx64"}},
    "Rev5 MacOSX Mountain Lion 10.8": {"run": {"machine": {"os": "mountain lion 10.10"}}, "build": {"platform": "macosx64"}},
    "Ubuntu ASAN VM large 12.04 x64": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64", "type": ["asan"]}},
    "Ubuntu ASAN VM 12.04 x64": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64", "type": ["asan"]}},
    "Ubuntu TSAN VM 12.04 x64": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64", "type": ["tsan"]}},
    "Ubuntu HW 12.04": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux32"}},
    "Ubuntu HW 12.04 x64": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux64"}},
    "Ubuntu VM 12.04": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux32"}},
    "Ubuntu VM 12.04 x64": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64"}},
    "Ubuntu VM large 12.04 x64": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64"}},
    "Ubuntu VM 12.04 x64 Mulet": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64", "type": ["mulet"]}},
    "Windows XP 32-bit": {"run": {"machine": {"os": "winxp"}}, "build": {"platform": "win32"}},
    "Windows 7 32-bit": {"run": {"machine": {"os": "win7"}}, "build": {"platform": "win32"}},
    "Windows 8 64-bit": {"run": {"machine": {"os": "win8"}}, "build": {"platform": "win64"}},
    "Windows 10 64-bit": {"run": {"machine": {"os": "win10"}}, "build": {"platform": "win64"}},
    "WINNT 5.2": {"run": {"machine": {"os": "winxp"}}, "build": {"platform": "win64"}},
    "WINNT 6.1 x86-64": {"run": {"machine": {"os": "win7"}}, "build": {"platform": "win64"}},
    "WINNT 6.2": {"run": {"machine": {"os": "win8"}}, "build": {"platform": "win64"}},
    "Win32 Mulet": {"run": {"machine": {"os": "winxp"}}, "build": {"platform": "win32", "type": ["mulet"]}},
}

STATUS_CODES = {
    0: "success",
    1: "warnings",
    2: "failure",
    3: "skipped",
    4: "exception",
    5: "retry",
    6: "cancelled",
    "0": "success",
    "1": "warnings",
    "2": "failure",
    "3": "skipped",
    "4": "exception",
    "5": "retry",
    "6": "cancelled",
    None: None,
    "success (0)": "success",
    "warnings (1)": "warnings",
    "failure (2)": "failure",
    "skipped (3)": "skipped",
    "exception (4)": "exception",
    "retry (5)": "retry",
    "cancelled (6)": "cancelled"
}

RATIO_PATTERN = re.compile(r"(\d+/\d+)")

ALLOWED_OS = [
    "android 2.3",
    "android 3.0",
    "android 4.0",
    "android 4.2",
    "android 4.3",
    "b2g",
    "lion 10.7",
    "macosx",
    "mountain lion 10.10",
    "snowleopard 10.6",
    "ubuntu",
    "winxp",
    "win7",
    "win8",
    "win10",
    "yosemite 10.10",
]

KNOWN_PLATFORM = {
    "android": {"build": {"platform": "android"}},
    "android-armv6": {"run": {"machine": {"type": "arm7"}}, "build": {"platform": "android"}},
    "android-debug": {"build": {"platform": "android", "type": ["debug"]}},
    "android-api-9": {"run": {"machine": {"os": "android 2.3"}}, "build": {"platform": "android"}},
    "android-api-9-debug": {"run": {"machine": {"os": "android 2.3"}}, "build": {"platform": "android", "type": ["debug"]}},
    "android-api-11": {"run": {"machine": {"os": "android 4.3"}}, "build": {"platform": "android"}},
    "android-api-11-debug": {"run": {"machine": {"os": "android 4.3"}}, "build": {"platform": "android", "type": ["debug"]}},
    "android-x86": {"run": {"type": "emulator"}, "build": {"platform": "android"}},
    "panda_android": {"run": {"type": "panda"}, "build": {"platform": "android"}},
    "b2g": {"run": {"machine": {"os": "b2g"}}, "build": {"platform": "b2g"}},
    "dolphin":{"run": {"machine": {"os": "b2g"}}, "build": {"platform": "dolphin"}},
    "dolphin_eng":{"run": {"machine": {"os": "b2g"}}, "build": {"platform": "dolphin"}},
    "dolphin-512":{"run": {"machine": {"os": "b2g", "memory": 512}}, "build": {"platform": "dolphin"}},
    "dolphin-512_eng":{"run": {"machine": {"os": "b2g", "memory": 512}}, "build": {"platform": "dolphin"}},
    "emulator": {"run": {"machine": {"type": "emulator"}}},
    "emulator-debug": {"run": {"machine": {"type": "emulator"}}, "build": {"type": ["debug"]}},
    "emulator-jb": {"run": {"machine": {"type": "emulator"}}, "build": {}},
    "emulator-jb-debug":{"run": {"machine": {"type": "emulator"}}, "build": {"type": ["debug"]}},
    "emulator-kk":{"run": {"machine": {"type": "emulator"}}, "build": {"platform": "flame"}},
    "emulator-kk-debug": {"run": {"machine": {"type": "emulator"}}, "build": {"platform": "flame", "type": ["debug"]}},
    "emulator-l": {"run": {"machine": {"type": "emulator"}}},
    "emulator-l-debug": {"run": {"machine": {"type": "emulator"}}, "build": {"type": ["debug"]}},
    "flame":{"build": {"platform": "flame"}},
    "flame_eng": {"build": {"platform": "flame"}},
    "flame-kk": {"build": {"platform": "flame"}},
    "flame-kk_eng": {"build": {"platform": "flame"}},
    "flame-kk_eng-debug": {"build": {"platform": "flame", "type": ["debug"]}},
    "hamachi": {"build": {"platform": "hamachi"}},
    "hamachi_eng": {"build": {"platform": "hamachi"}},
    "helix": {"build": {"platform": "helix"}},
    "l10n": {},
    "linux": {"build": {"platform": "linux32"}},
    "linux-debug": {"build": {"platform": "linux32", "type": ["debug"]}},
    "linux32_gecko": {"build": {"platform": "linux32", "type": ["debug"]}},
    "linux32_gecko-debug": {"build": {"platform": "linux32", "type": ["debug"]}},
    "linux32_gecko_localizer": {},
    "linux64": {"build": {"platform": "linux64"}},
    "linux64-asan": {"build": {"platform": "linux64", "type": ["asan"]}},
    "linux64-asan-debug": {"build": {"platform": "linux64", "type": ["asan", "debug"]}},
    "linux64-b2g-haz": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "linux64-br-haz": {"build": {"platform": "linux64"}},
    "linux64-debug": {"build": {"platform": "linux64", "type": ["debug"]}},
    "linux64_gecko": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "linux64_gecko-debug": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g", "type": ["debug"]}},
    "linux64_gecko_localizer": {},
    "linux64_graphene": {"run": {"machine": {"vm": "graphene"}}, "build": {"platform": "linux64"}},
    "linux64_horizon": {"run": {"machine": {"vm": "horizon"}}, "build": {"platform": "linux64"}},
    "linux64-tsan": {"build": {"platform": "linux64", "type": ["tsan"]}},
    "linux64-mulet": {"build": {"platform": "linux64", "type": ["mulet"]}},
    "linux64-st-an-debug": {"build": {"platform": "linux64", "type": ["debug"]}},
    "linux64-sh-haz": {"build": {"platform": "linux64", "type": ["debug"]}},
    "macosx64": {"build": {"platform": "macosx64"}},
    "macosx64-debug": {"build": {"platform": "macosx64", "type": ["debug"]}},
    "macosx64_gecko": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "macosx64_gecko-debug": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g", "type": "debug"}},
    "macosx64_gecko_localizer": {},
    "macosx64_graphene": {"run": {"machine": {"vm": "graphene"}}, "build": {"platform": "macosx64"}},
    "macosx64_horizon": {"run": {"machine": {"vm": "horizon"}}, "build": {"platform": "macosx64"}},
    "macosx64-lion": {"run": {"machine": {"os": "lion 10.7"}}, "build": {"platform": "macosx64"}},
    "macosx64-mulet": {"build": {"platform": "macosx64", "type": ["mulet"]}},
    "macosx64-st-an-debug": {"build": {"platform": "macosx64", "type": ["debug"]}},
    "mock": {"build": {"platform": "mock"}},
    "mountainlion": {"run": {"machine": {"os": "mountain lion 10.10"}}, "build": {"platform": "macosx64"}},
    "nexus-4": {"build": {"platform": "nexus4"}},
    "nexus-4_eng": {"build": {"platform": "nexus4"}},
    "nexus-5-l": {"build": {"platform": "nexus5"}},
    "nexus-5-l_eng": {"build": {"platform": "nexus5"}},
    "snowleopard": {"run": {"machine": {"os": "snowleopard 10.6"}}, "build": {"platform": "macosx64"}},

    "ubuntu32_hw": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux32"}},
    "ubuntu32_vm": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux32"}},
    "ubuntu64-asan_vm":{"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64", "type": ["asan"]}},
    "ubuntu64_hw": {"run": {"machine": {"os": "ubuntu"}}, "build": {"platform": "linux64"}},
    "ubuntu64_vm": {"run": {"machine": {"os": "ubuntu", "type": "vm"}}, "build": {"platform": "linux64"}},
    "wasabi":{"run": {"machine": {"os": "b2g"}}, "build": {"platform": "wasabi"}},
    "win32": {"build": {"platform": "win32"}},
    "win32-debug": {"build": {"platform": "win32", "type": ["debug"]}},
    "win32-mulet": {"build": {"platform": "win32", "type": ["mulet"]}},
    "win32_gecko": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g"}},
    "win32_gecko-debug": {"run": {"machine": {"os": "b2g", "type": "emulator"}}, "build": {"platform": "b2g", "type": ["debug"]}},
    "win32_gecko_localizer":{},
    "win64": {"build": {"platform": "win64"}},
    "win64-debug": {"build": {"platform": "win64", "type": ["debug"]}},
    "win64_graphene": {"run": {"machine": {"vm": "graphene"}}, "build": {"platform": "win64"}},
    "win64_horizon": {"run": {"machine": {"vm": "horizon"}}, "build": {"platform": "win64"}},
    "win64-rev2": {"build": {"platform": "win64"}},
    "win7-ix": {"run": {"machine": {"os": "win7"}}, "build": {"platform": "win64"}},
    "win8": {"run": {"machine": {"os": "win8"}}, "build": {"platform": "win64"}},
    "win8_64": {"run": {"machine": {"os": "win8"}}, "build": {"platform": "win64"}},
    "win10_64": {"run": {"machine": {"os": "win10"}}, "build": {"platform": "win64"}},
    "xp-ix": {"run": {"machine": {"os": "winxp"}}, "build": {"platform": "win32"}},
    "yosemite": {"run": {"machine": {"os": "yosemite 10.10"}}, "build": {"platform": "macosx64"}}
}

ALLOWED_PLATFORMS = [
    "android",
    "b2g",
    "flame",
    "linux32",
    "linux64",
    "l10n",  # FOR repack
    "macosx32",
    "macosx64",
    "mock",  # FOR fuzzing
    "win32",
    "win64"
]


ALLOWED_PRODUCTS = [
    "b2g",
    "fennec",
    "firefox",
    "fuzzing",
    "jetpack",
    "mobile",
    "spidermonkey",
    "thunderbird",
    "xulrunner"
]