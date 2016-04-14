# encoding: utf-8
#
# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this file,
# You can obtain one at http://mozilla.org/MPL/2.0/.
#
# Author: Trung Do (chin.bimbo@gmail.com)
#
from __future__ import division
from __future__ import unicode_literals

import json

import taskcluster

from mohg.repos.changesets import Changeset
from mohg.repos.revisions import Revision
from pyLibrary import convert
from pyLibrary.debugs.logs import Log
from pyLibrary.dot import wrap, Dict
from pyLibrary.env import http
from pyLibrary.jsons import stream
from pyLibrary.times.dates import Date
from testlog_etl.transforms import EtlHeadGenerator


def process(source_key, source, destination, resources, please_stop=None):
    """
    This transform will turn a pulse message containing info about a jscov artifact on taskcluster
    into a list of records of line coverages. Each record represents a line.

    :param source_key: The key of the file containing the pulse messages in the source pulse message bucket
    :param source: The source pulse messages, in a batch of (usually) 100
    :param destination: The destination for the transformed data
    :param resources: not used
    :param please_stop: The stop signal to stop the current thread
    :return: The list of keys of files in the destination bucket
    """
    keys = []
    etl_header_gen = EtlHeadGenerator(source_key)
    Log.note("Processing " + source_key)
    bucket_file_count = -1

    for msg_line_index, msg_line in enumerate(source.read_lines()):
        if please_stop:
            Log.error("Shutdown detected. Stopping job ETL.")

        pulse_record = convert.json2value(msg_line)
        artifact_file_name = pulse_record.artifact.name

        # we're only interested in jscov files, at lease at the moment
        if "jscov" not in artifact_file_name:
            continue

        # create the key for the file in the bucket, and add it to a list to return later
        bucket_file_count += 1
        bucket_key = source_key + "." + unicode(bucket_file_count)
        keys.append(bucket_key)

        # construct the artifact's full url
        taskId = pulse_record.status.taskId
        runId = pulse_record.runId
        full_artifact_path = "https://public-artifacts.taskcluster.net/" + taskId + "/" + unicode(runId) + "/" + artifact_file_name

        # get the task definition
        queue = taskcluster.Queue()
        task_definition = wrap(queue.task(taskId=taskId))

        # get additional info
        repo = get_revision_info(task_definition, resources)
        run = get_run_info(task_definition)
        build = get_build_info(task_definition)

        # fetch the artifact
        Log.note("Processing " + full_artifact_path)
        response_stream = http.get(full_artifact_path).raw

        # TODO:  Add a timer around this so we see how long it takes
        records = []
        for source_file_index, obj in enumerate(stream.parse(response_stream, [], ["."])):
            obj = wrap(obj)
            if please_stop:
                Log.error("Shutdown detected. Stopping job ETL.")

            if source_file_index == 0:
                # this is not a jscov object but an object containing the version metadata
                # TODO: this metadata should not be here
                # TODO: this version info is not used right now. Make use of it later.
                jscov_format_version = obj.version
                continue

            # get the test name. Just use the test file name at the moment
            # TODO: change this when needed
            test_name = obj.testUrl.split("/")[-1]

            # a variable to count the number of lines so far for this source file
            count = 0

            # turn obj.covered (a list) into a set for use later
            file_covered = set(obj.covered)

            # file-level info
            file_info = wrap({
                "name": obj.sourceFile,
                "covered": [{"line": c} for c in obj.covered],
                "uncovered": [{"line": c} for c in obj.uncovered],
                "total_covered": len(obj.covered),
                "total_uncovered": len(obj.uncovered),
                "percentage_covered": len(obj.covered) / (len(obj.covered) + len(obj.uncovered))
            })

            # orphan lines (i.e. lines without a method), initialized to all lines
            orphan_covered = set(obj.covered)
            orphan_uncovered = set(obj.uncovered)

            # iterate through the methods of this source file
            for method_name, method_lines in obj.methods.iteritems():
                _, dest_etl = etl_header_gen.next(pulse_record.etl, source_file_index)

                # reusing dest_etl.id, which should be continuous
                record_key = bucket_key + "." + unicode(dest_etl.id)

                all_method_lines_set = set(method_lines)
                method_covered = all_method_lines_set & file_covered
                method_uncovered = all_method_lines_set - method_covered
                method_percentage_covered = len(method_covered) / len(all_method_lines_set)

                orphan_covered = orphan_covered - method_covered
                orphan_uncovered = orphan_uncovered - method_uncovered

                new_record = wrap({
                    "test": {
                        "name": test_name,
                        "url": obj.testUrl
                    },
                    "source": {
                        "file": file_info,
                        "method": {
                            "name": method_name,
                            "covered": [{"line": c} for c in method_covered],
                            "uncovered": [{"line": c} for c in method_uncovered],
                            "total_covered": len(method_covered),
                            "total_uncovered": len(method_uncovered),
                            "percentage_covered": method_percentage_covered,
                        }
                    },
                    "etl": dest_etl,
                    "repo": repo,
                    "run": run,
                    "build": build
                })

                # file marker
                if count == 0:
                    new_record.source.is_file = "true"

                records.append({"id": record_key, "value": new_record})
                count += 1

            # a record for all the lines that are not in any method
            if len(orphan_covered) + len(orphan_uncovered) > 0:
                _, dest_etl = etl_header_gen.next(pulse_record.etl, source_file_index)
                record_key = bucket_key + "." + unicode(dest_etl.id)
                new_record = wrap({
                    "test": {
                        "name": test_name,
                        "url": obj.testUrl
                    },
                    "source": {
                        "file": file_info,
                        "method": {
                            "covered": [{"line": c} for c in orphan_covered],
                            "uncovered": [{"line": c} for c in orphan_uncovered],
                            "total_covered": len(orphan_covered),
                            "total_uncovered": len(orphan_uncovered),
                            "percentage_covered": len(orphan_covered) / (len(orphan_covered) + len(orphan_uncovered)),
                        }
                    },
                    "etl": dest_etl,
                    "repo": repo,
                    "run": run,
                    "build": build
                })

                # file marker
                if count == 0:
                    new_record.source.is_file = "true"

                records.append({"id": record_key, "value": new_record})
                count += 1

        destination.extend(records)
    return keys


def get_revision_info(task_definition, resources):
    """
    Get the changeset, revision and push info for a given task in TaskCluster

    :param task_definition: The task definition
    :param resources: Pass this from the process method
    :return: The repo object containing information about the changeset, revision and push
    """

    # head_repo will look like "https://hg.mozilla.org/try/"
    head_repo = task_definition.payload.env.GECKO_HEAD_REPOSITORY
    branch = head_repo.split("/")[-2]

    revision = task_definition.payload.env.GECKO_HEAD_REV
    rev = Revision(branch={"name": branch}, changeset=Changeset(id=revision))
    repo = resources.hg.get_revision(rev)
    return repo


def get_run_info(task_definition):
    """
    Get the run object that contains properties that describe the run of this job

    :param task_definition: The task definition
    :return: The run object
    """
    run = Dict()
    run.suite = task_definition.extra.suite
    run.chunk = task_definition.extra.chunks.current
    return run


def get_build_info(task_definition):
    """
    Get a build object that describes the build

    :param task_definition: The task definition
    :return: The build object
    """
    build = Dict()
    build.platform = task_definition.extra.treeherder.build.platform

    # head_repo will look like "https://hg.mozilla.org/try/"
    head_repo = task_definition.payload.env.GECKO_HEAD_REPOSITORY
    branch = head_repo.split("/")[-2]
    build.branch = branch

    build.revision = task_definition.payload.env.GECKO_HEAD_REV
    build.revision12 = build.revision[0:12]

    # MOZILLA_BUILD_URL looks like this:
    # "https://queue.taskcluster.net/v1/task/e6TfNRfiR3W7ZbGS6SRGWg/artifacts/public/build/target.tar.bz2"
    build.url = task_definition.payload.env.MOZILLA_BUILD_URL

    # get the taskId of the build, then from that get the task definition of the build
    # note: this is a fragile way to get the taskId of the build
    build.taskId = build.url.split("/")[5]
    queue = taskcluster.Queue()
    build_task_definition = wrap(queue.task(taskId=build.taskId))
    build.name = build_task_definition.extra.build_name
    build.product = build_task_definition.extra.build_product
    build.type = build_task_definition.extra.build_type  #TODO: expand "dbg" to "debug"
    build.created_timestamp = Date(build_task_definition.created).unix

    return build
