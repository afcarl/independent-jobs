"""
Copyright (c) 2013-2014 Heiko Strathmann
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:
 *
1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
 *
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES
(INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES;
LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND
ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
The views and conclusions contained in the software and documentation are those
of the authors and should not be interpreted as representing official policies,
either expressed or implied, of the author.
"""
from abc import abstractmethod
import logging
from os import makedirs
import os
from popen2 import popen2
import time

from independent_jobs.aggregators.PBSResultAggregatorWrapper import PBSResultAggregatorWrapper
from independent_jobs.engines.IndependentComputationEngine import IndependentComputationEngine
from independent_jobs.tools.FileSystem import FileSystem
from independent_jobs.tools.Serialization import Serialization


class Dispatcher(object):
    @staticmethod
    def dispatch(filename):
        job = Serialization.deserialize_object(filename)
        job.compute()

class BatchClusterComputationEngine(IndependentComputationEngine):
    def __init__(self, batch_parameters, submission_cmd,
                 check_interval=10, do_clean_up=False):
        IndependentComputationEngine.__init__(self)
        
        self.batch_parameters = batch_parameters
        self.check_interval = check_interval
        self.do_clean_up = do_clean_up
        self.submission_cmd = submission_cmd
        # make sure submission command executable is in path
        if not FileSystem.cmd_exists("qsub"):
            raise ValueError("Submission command executable \"%s\" not found" % submission_cmd)
        
        self.submitted_job_map = {}
        self.submitted_job_counter = 0
    
    def get_aggregator_filename(self, job_name):
        job_folder = self.get_job_foldername(job_name)
        return os.sep.join([job_folder, "aggregator.bin"])
    
    def get_job_foldername(self, job_name):
        return os.sep.join([self.batch_parameters.foldername, job_name])
    
    def get_job_filename(self, job_name):
        return os.sep.join([self.get_job_foldername(job_name), "job.bin"])
    
    @abstractmethod
    def create_batch_script(self, job_name, dispatcher_string):
        raise NotImplementedError()
        
    def submit_wrapped_pbs_job(self, wrapped_job, job_name):
        job_folder = self.get_job_foldername(job_name)
        
        # track submitted jobs/aggregators for retrieving results from FS later
        # dont do this in memory as it might blow up things
        self.submitted_job_map[job_name] = False
        self.submitted_job_counter += 1
        
        # try to create folder if not yet exists
        job_filename = self.get_job_filename(job_name)
        logging.info("Creating job with file %s" % job_filename)
        try:
            makedirs(job_folder)
        except OSError:
            pass
        
        Serialization.serialize_object(wrapped_job, job_filename)
        
        # allow the FS and queue to process things        
        time.sleep(.5)
        
        # wait until FS says that the file exists
        while not FileSystem.file_exists_new_shell(job_filename):
            time.sleep(1)
        
        lines = []
        lines.append("from engines.BatchClusterComputationEngine import Dispatcher")
        lines.append("from tools.Log import Log")
        lines.append("Log.set_loglevel(%d)" % self.batch_parameters.loglevel)
        lines.append("filename=\"%s\"" % job_filename)
        lines.append("Dispatcher.dispatch(filename)")
        
        dispatcher_string = "python -c '" + os.linesep.join(lines) + "'"
        
        job_string = self.create_batch_script(job_name, dispatcher_string)
        
        # put the custom parameter string in front if existing
        if self.batch_parameters.parameter_prefix != "":
            job_string = os.linesep.join([self.batch_parameters.parameter_prefix,
                                         job_string])
        
        f = open(job_folder + os.sep + "pbs_script", "w")
        f.write(job_string)
        f.close()
        
        # send job_string to qsub
        outpipe, inpipe = popen2('qsub')
        inpipe.write(job_string + os.linesep)
        inpipe.close()
        
        job_id = outpipe.read().strip()
        outpipe.close()
        
        if job_id == "":
            raise RuntimeError("Could not parse job_id. Something went wrong with the job submission")
        
        f = open(job_folder + os.sep + "job_id", 'w')
        f.write(job_id + os.linesep)
        f.close()
    
    def create_job_name(self):
        return FileSystem.get_unique_filename(self.batch_parameters.job_name_base)
    
    def submit_job(self, job):
        # replace job's wrapped_aggregator by PBS wrapped_aggregator to allow
        # FS based communication
        
        # use a unique job name, but check that this folder doesnt yet exist
        job_name = self.create_job_name()
        
        aggregator_filename = self.get_aggregator_filename(job_name)
        job.aggregator = PBSResultAggregatorWrapper(job.aggregator,
                                                    aggregator_filename,
                                                    job_name,
                                                    self.do_clean_up)
        
        self.submit_wrapped_pbs_job(job, job_name)
        
        return job.aggregator
    
    def wait_for_all(self):
        """
        Waits for all jobs to be completed, which means that until all
        result files of all submitted jobs exist. Afterwards, the job list is
        emptied for another trial.
        """
        # check whether there are unfinished jobs
        waiting_start = time.time()
        
        # loop while there are unfinished jobs
        while False in self.submitted_job_map.viewvalues():
            
            # iterate over all jobs in list and check whether they are done yet
            for job_name, job_finished in self.submitted_job_map.iteritems():
                filename = self.get_aggregator_filename(job_name)
                
                # do not wait again for finished jobs
                if not job_finished:
                    logging.info("waiting for %s" % job_name)
                
                    # wait until file exists (dangerous, so have a maximum waiting time
                    # after which old job is discarded and replacement is submitted)
                    while True:
                        # race condition is fine here, but use a new python shell
                        # due to NFS cache problems otherwise
                        if FileSystem.file_exists_new_shell(filename):
                            self.submitted_job_map[job_name] = True
                            break
                        
                        time.sleep(self.check_interval)
                        
                        # check whether maximum waiting time is over and re-submit if is
                        waited_for = time.time() - waiting_start
                        if waited_for > self.batch_parameters.max_walltime:
                            new_job_name = self.create_job_name()
                            logging.info("%s exceeded maximum waiting time of %d" % (job_name, self.batch_parameters.max_walltime))
                            logging.info("Re-submitting under name %s" % new_job_name)

                            # remove from submitted list to not wait anymore and
                            # change job name
                            del self.submitted_job_map[job_name]
                            job_filename = self.get_job_filename(job_name)
                            
                            # load job from disc and re-submit
                            wrapped_job = Serialization.deserialize_object(job_filename)
                            self.submit_wrapped_pbs_job(wrapped_job, new_job_name)
                            waiting_start = time.time()
                            
                            # submitted job map has changed, break inner
                            # infinite loop and start again
                            break
            
        logging.info("All jobs finished.")

        # reset internal list for new submission round
        self.submitted_aggregator_filenames = []
        