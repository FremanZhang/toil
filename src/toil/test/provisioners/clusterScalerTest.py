# Copyright (C) 2015-2016 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from builtins import map
from builtins import range
from builtins import object
from past.utils import old_div
import time
from contextlib import contextmanager
from threading import Thread, Event
import logging
import random

# Python 3 compatibility imports
from six.moves.queue import Empty, Queue
from six.moves import xrange
from six import iteritems

from bd2k.util.objects import InnerClass

from toil.job import JobNode
from toil.provisioners import Node
from toil.test import ToilTest, slow
from toil.batchSystems.abstractBatchSystem import (AbstractScalableBatchSystem,
                                                   NodeInfo,
                                                   AbstractBatchSystem)
from toil.provisioners.abstractProvisioner import AbstractProvisioner, Shape
from toil.provisioners.clusterScaler import ClusterScaler, binPacking
from toil.common import Config


logger = logging.getLogger(__name__)

# FIXME: Temporary log stuff - needs to be move to test setup

if False:
    logger.setLevel(logging.DEBUG)
    import sys

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logging.getLogger().addHandler(ch)


class ClusterScalerTest(ToilTest):
    def testBinPacking(self):
        """
        Tests the bin-packing method used by the cluster scaler.
        """
        for test in range(50):
            nodeShape = Shape(wallTime=random.choice(list(range(1, 100))),
                              memory=random.choice(list(range(1, 10))),
                              cores=random.choice(list(range(1, 10))),
                              disk=random.choice(list(range(1, 10))))
            randomJobShape = lambda x: Shape(wallTime=random.choice(list(range(1, (3 * x.wallTime) + 1))),
                                             memory=random.choice(list(range(1, x.memory + 1))),
                                             cores=random.choice(list(range(1, x.cores + 1))),
                                             disk=random.choice(list(range(1, x.disk + 1))))
            numberOfJobs = random.choice(list(range(1, 1000)))
            randomJobShapes = [randomJobShape(nodeShape) for i in range(numberOfJobs)]
            startTime = time.time()
            numberOfBins = binPacking(randomJobShapes, nodeShape)
            logger.info("For node shape %s and %s job-shapes got %s bins in %s seconds, %s jobs/bin" % 
                        (nodeShape, numberOfJobs, numberOfBins, time.time() - startTime, old_div(float(numberOfJobs),numberOfBins)))

    def _testClusterScaling(self, config, numJobs, numPreemptableJobs):
        """
        Test the ClusterScaler class with different patterns of job creation. Tests ascertain
        that autoscaling occurs and that all the jobs are run.
        """
        # First do simple test of creating 100 preemptable and non-premptable jobs and check the
        # jobs are completed okay, then print the amount of worker time expended and the total
        # number of worker nodes used.

        logger.info("Creating dummy batch system and scalar")

        mock = MockBatchSystemAndProvisioner(config, secondsPerJob=2.0)
        clusterScaler = ClusterScaler(mock, mock, config)
        clusterScaler.start()
        try:
            # Add 100 jobs to complete 
            logger.info("Creating test jobs")
            list(map(lambda x: mock.addJob(), list(range(numJobs))))
            list(map(lambda x: mock.addJob(preemptable=True), list(range(numPreemptableJobs))))
    
            # Add some completed jobs
            for preemptable in (True, False):
                if preemptable and numPreemptableJobs > 0 or not preemptable and numJobs > 0:
                    # Add a 1000 random jobs
                    for i in range(1000):
                        x = mock.getNodeShape(preemptable)
                        iJ = JobNode(jobStoreID=1,
                                     requirements=dict(memory=random.choice(list(range(1, x.memory))),
                                                       cores=random.choice(list(range(1, x.cores))),
                                                       disk=random.choice(list(range(1, x.disk))),
                                                       preemptable=preemptable),
                                     command=None,
                                     jobName='testClusterScaling', unitName='')
                        clusterScaler.addCompletedJob(iJ, random.choice(list(range(1, x.wallTime))))
    
            logger.info("Waiting for jobs to be processed")
            startTime = time.time()
            # Wait while the cluster the process chunks through the jobs
            while (mock.getNumberOfJobsIssued(preemptable=False) > 0
                   or mock.getNumberOfJobsIssued(preemptable=True) > 0
                   or mock.getNumberOfNodes() > 0 or mock.getNumberOfNodes(preemptable=True) > 0):
                logger.info("Running, non-preemptable queue size: %s, non-preemptable workers: %s, "
                            "preemptable queue size: %s, preemptable workers: %s" %
                            (mock.getNumberOfJobsIssued(preemptable=False),
                             mock.getNumberOfNodes(preemptable=False),
                             mock.getNumberOfJobsIssued(preemptable=True),
                             mock.getNumberOfNodes(preemptable=True)))
                clusterScaler.check()
                time.sleep(0.5)
            logger.info("We waited %s for cluster to finish" % (time.time() - startTime))
        finally:
            clusterScaler.shutdown()

        # Print some info about the autoscaling
        for i, bs in enumerate(mock.delegates):
            preemptable = bool(i)
            logger.info("Preemptable: %s, Total-jobs: %s: Max-workers: %s,"
                        " Total-worker-time: %s, Worker-time-per-job: %s" %
                        (preemptable, bs.totalJobs, bs.maxWorkers,
                         bs.totalWorkerTime,
                         old_div(bs.totalWorkerTime, bs.totalJobs) if bs.totalJobs > 0 else 0.0))

    @slow
    def testClusterScaling(self):
        """
        Test scaling for a batch of non-preemptable jobs and no preemptable jobs (makes debugging
        easier).
        """
        config = Config()

        # Make defaults dummy values
        config.defaultMemory = 1
        config.defaultCores = 1
        config.defaultDisk = 1

        # No preemptable nodes/jobs
        config.maxPreemptableNodes = 0  # No preemptable nodes

        # Non-preemptable parameters
        config.nodeType = Shape(20, 10, 10, 10)
        config.minNodes = 0
        config.maxNodes = 10

        # Algorithm parameters
        config.alphaPacking = 0.8
        config.betaInertia = 1.2
        config.scaleInterval = 3

        self._testClusterScaling(config, numJobs=100, numPreemptableJobs=0)

    @slow
    def testClusterScalingWithPreemptableJobs(self):
        """
        Test scaling simultaneously for a batch of preemptable and non-preemptable jobs.
        """
        config = Config()

        # Make defaults dummy values
        config.defaultMemory = 1
        config.defaultCores = 1
        config.defaultDisk = 1

        # Preemptable node parameters
        config.nodeType = Shape(20, 10, 10, 10)
        config.minNodes = 0
        config.maxNodes = 10

        # Preemptable node parameters
        config.preemptableNodeType = Shape(20, 10, 10, 10)
        config.minPreemptableNodes = 0
        config.maxPreemptableNodes = 10

        # Algorithm parameters
        config.alphaPacking = 0.8
        config.betaInertia = 1.2
        config.scaleInterval = 3

        self._testClusterScaling(config, numJobs=100, numPreemptableJobs=100)


# noinspection PyAbstractClass
class MockBatchSystemAndProvisioner(AbstractScalableBatchSystem, AbstractProvisioner):
    """
    Mimics a job batcher, provisioner and scalable batch system
    """

    def __init__(self, config, secondsPerJob):
        super(MockBatchSystemAndProvisioner, self).__init__(config=config)
        # To mimic parallel preemptable and non-preemptable queues
        # for jobs we create two parallel instances of the following class
        self.config = config
        self.secondsPerJob = secondsPerJob
        self.delegates = [self.Delegate(preemptable=False), self.Delegate(preemptable=True)]
        self.provisioner = self
        self.batchSystem = self


    def _pick(self, preemptable=False):
        """
        Select the preemptable or non-premptable delegate
        """
        return self.delegates[int(preemptable)]

    def addJob(self, preemptable=False):
        self._pick(preemptable).addJob()

    # Leader methods

    def getNumberOfJobsIssued(self, preemptable=False):
        return self._pick(preemptable).getNumberOfJobsIssued()
    
    def getNumberAndAvgRuntimeOfCurrentlyRunningJobs(self):
        return self.getNumberOfJobsIssued(), 50 

    # Stub out all AbstractBatchSystem methods since they are never called

    for name, value in iteritems(AbstractBatchSystem.__dict__):
        if getattr(value, '__isabstractmethod__', False):
            exec('def %s(): pass' % name)
        # Without this, the class would end up with .name and .value attributes
        del name, value

    # AbstractScalableBatchSystem methods

    def getNodes(self, preemptable=False, timeout=None):
        return self._pick(preemptable).getNodes()

    def nodeInUse(self, nodeIP):
        return False

    @contextmanager
    def nodeFiltering(self, filter):
        nodes = self.getProvisionedWorkers(True) + self.getProvisionedWorkers(False)
        yield nodes

    # AbstractProvisioner methods

    def getNodeShape(self, preemptable=False):
        return self.config.preemptableNodeType if preemptable else self.config.nodeType

    def addNodes(self, numNodes, preemptable):
        self._pick(preemptable)._addNodes(numNodes=numNodes)
        return self.getNumberOfNodes(preemptable)

    def getProvisionedWorkers(self, preemptable):
        """
        Returns a list of Node objects, each representing a worker node in the cluster

        :param preemptable: If True only return preemptable nodes else return non-preemptable nodes
        :return: list of Node
        """
        return list(self._pick(preemptable).nodesToWorker.keys())

    def terminateNodes(self, nodes):
        for preemptable in (True, False):
            self._pick(preemptable)._removeNodes(nodes)

    def remainingBillingInterval(self, node):
        pass

    # FIXME: Not part of AbstractScalableBatchSystem but used by the tests

    def getNumberOfNodes(self, preemptable=False):
        return self._pick(preemptable).getNumberOfNodes()

    @InnerClass
    class Delegate(object):
        """
        Implements the methods of the Provisioner and JobBatcher class needed for the
        ClusterScaler class.
        """

        def __init__(self, preemptable):
            super(MockBatchSystemAndProvisioner.Delegate, self).__init__()
            self.jobQueue = Queue()
            self.totalJobs = 0  # Count of total jobs processed
            self.totalWorkerTime = 0.0  # Total time spent in worker threads
            self.nodesToWorker = {}  # Map from Node to instances of the Worker class
            self.maxWorkers = 0  # Maximum number of workers
            self.preemptable = preemptable

        def addJob(self):
            """
            Add a job to the job queue
            """
            self.totalJobs += 1
            self.jobQueue.put(None)

        # JobBatcher functionality

        def getNumberOfJobsIssued(self):
            return self.jobQueue.qsize()

        # AbstractScalableBatchSystem functionality

        def getNodes(self):
            nodes = dict()
            for i, worker in enumerate(self.nodesToWorker.values()):
                nodes[(i, self.preemptable)] = NodeInfo(coresTotal=0, coresUsed=0, requestedCores=1,
                                                        memoryTotal=0, memoryUsed=0, requestedMemory=1,
                                                        workers=1 if worker.busyEvent.is_set() else 0)
            return nodes

        def _addNodes(self, numNodes):
            class Worker(object):
                def __init__(self, jobQueue, secondsPerJob):
                    self.busyEvent = Event()
                    self.stopEvent = Event()

                    def workerFn():
                        while True:
                            if self.stopEvent.is_set():
                                return
                            try:
                                jobQueue.get(timeout=1.0)
                            except Empty:
                                continue
                            self.busyEvent.set()
                            time.sleep(secondsPerJob)
                            self.busyEvent.clear()

                    self.startTime = time.time()
                    self.worker = Thread(target=workerFn)
                    self.worker.start()

                def stop(self):
                    self.stopEvent.set()
                    self.worker.join()
                    return time.time() - self.startTime

            for i in range(numNodes):
                node = Node('127.0.0.1', '127.0.0.1', 'testNode', time.time())
                self.nodesToWorker[node] = Worker(self.jobQueue, self.outer.secondsPerJob)
            self.maxWorkers = max(self.maxWorkers, len(self.nodesToWorker))

        def _removeNodes(self, nodes):
            logger.info("removing nodes. %s workers and %s to terminate", len(self.nodesToWorker), len(nodes))
            for node in nodes:
                logger.info("removed node")
                try:
                    worker = self.nodesToWorker.pop(node)
                    self.totalWorkerTime += worker.stop()
                except KeyError:
                    # Node isn't our responsibility
                    pass

        def getNumberOfNodes(self):
            return len(self.nodesToWorker)
