#!/usr/bin/env python
# -*- coding: utf-8 -*-

import random
import time
import traceback
from collections import defaultdict, deque
import copy
import pickle
import psutil
import os
from os import path
import numpy as np
import networkx as nx
import pandas as pd
import pyquaternion
import geometry_msgs

import rospy
import tf2_ros
from actionlib import SimpleActionClient
from actionlib_msgs.msg import GoalStatus
from move_base_msgs.msg import MoveBaseGoal, MoveBaseAction
from gazebo_msgs.msg import ModelState
from gazebo_msgs.srv import SetModelState
from geometry_msgs.msg import PoseWithCovarianceStamped, Pose, Quaternion, PoseStamped, TransformStamped
from nav_msgs.msg import Odometry, Path
from sensor_msgs.msg import LaserScan
from std_srvs.srv import Empty

from performance_modelling_py.environment import ground_truth_map
from performance_modelling_py.utils import backup_file_if_exists, print_info, print_error


class RunFailException(Exception):
    pass


def main():
    rospy.init_node('slam_benchmark_supervisor', anonymous=False)

    node = None

    # noinspection PyBroadException
    try:
        node = GlobalPlanningBenchmarkSupervisor()
        node.start_run()
        rospy.spin()

    except KeyboardInterrupt:
        node.ros_shutdown_callback()
    except RunFailException as e:
        print_error(e)
    except Exception:
        print_error(traceback.format_exc())

    finally:
        if node is not None:
            node.end_run()


class GlobalPlanningBenchmarkSupervisor:
    def __init__(self):

        # topics, services, actions, entities and frames names
        ground_truth_pose_topic = rospy.get_param('~ground_truth_pose_topic')
        #initial_pose_topic = rospy.get_param('~initial_pose_topic')
        #pause_physics_service = rospy.get_param('~pause_physics_service')
        #unpause_physics_service = rospy.get_param('~unpause_physics_service')
        #set_entity_state_service = rospy.get_param('~set_entity_state_service')
        #global_localization_service = rospy.get_param('~global_localization_service')
        navigate_to_pose_action = rospy.get_param('~navigate_to_pose_action')
        self.fixed_frame = rospy.get_param('~fixed_frame')
        self.robot_base_frame = rospy.get_param('~robot_base_frame')
        self.robot_entity_name = rospy.get_param('~robot_entity_name')
        self.robot_radius = rospy.get_param('~robot_radius')

        # file system paths
        self.run_output_folder = rospy.get_param('~run_output_folder')
        self.benchmark_data_folder = path.join(self.run_output_folder, "benchmark_data")
        self.ps_output_folder = path.join(self.benchmark_data_folder, "ps_snapshots")
        self.ground_truth_map_info_path = rospy.get_param('~ground_truth_map_info_path') 

        # run parameters
        run_timeout = rospy.get_param('~run_timeout')  #when the robot stucked we can use time out so according to time out we can finish the run
        #self.waypoint_timeout = rospy.get_param('~waypoint_timeout')
        #ps_snapshot_period = rospy.get_param('~ps_snapshot_period')
        #self.ps_pid_father = rospy.get_param('~pid_father')
        #self.ps_processes = psutil.Process(self.ps_pid_father).children(recursive=True)  # list of processes children of the benchmark script, i.e., all ros nodes of the benchmark including this one
        self.ground_truth_map = ground_truth_map.GroundTruthMap(self.ground_truth_map_info_path)
        #self.initial_pose_covariance_matrix = np.zeros((6, 6), dtype=float)
        #self.initial_pose_covariance_matrix[0, 0] = rospy.get_param('~initial_pose_std_xy')**2
        #self.initial_pose_covariance_matrix[1, 1] = rospy.get_param('~initial_pose_std_xy')**2
        #self.initial_pose_covariance_matrix[5, 5] = rospy.get_param('~initial_pose_std_theta')**2
        #self.goal_tolerance = rospy.get_param('~goal_tolerance')

        # run variables
        self.initial_pose = None
        self.traversal_path_poses = None
        self.current_goal = None
        self.num_goals = None
        self.goal_sent_count = 0
        self.path_recieve = False
        #self.run_started = False
        #self.latest_ground_truth_pose_msg = None
        #self.goal_succeeded_count = 0
        #self.goal_failed_count = 0
        #self.goal_rejected_count = 0
        

        # prepare folder structure
        if not path.exists(self.benchmark_data_folder):
            os.makedirs(self.benchmark_data_folder)

        if not path.exists(self.ps_output_folder):
            os.makedirs(self.ps_output_folder)

        # file paths for benchmark data    #we are using it for collecting benchmark data #in my case we can just keep path for example
        #self.estimated_poses_file_path = path.join(self.benchmark_data_folder, "estimated_poses.csv")
        #self.estimated_correction_poses_file_path = path.join(self.benchmark_data_folder, "estimated_correction_poses.csv")
        self.ground_truth_poses_file_path = path.join(self.benchmark_data_folder, "ground_truth_poses.csv")
        #self.scans_file_path = path.join(self.benchmark_data_folder, "scans.csv")
        self.run_events_file_path = path.join(self.benchmark_data_folder, "run_events.csv")  #I keep it for example
        self.init_run_events_file()

        self.voronoi_graph_node_finder()

        # pandas dataframes for benchmark data   #not useful right now
        #self.estimated_poses_df = pd.DataFrame(columns=['t', 'x', 'y', 'theta'])
        #self.estimated_correction_poses_df = pd.DataFrame(columns=['t', 'x', 'y', 'theta', 'cov_x_x', 'cov_x_y', 'cov_y_y', 'cov_theta_theta'])
        self.ground_truth_poses_df = pd.DataFrame(columns=['t', 'x', 'y', 'theta', 'v_x', 'v_y', 'v_theta'])  

        # setup timers
        self.tfTimer = rospy.Timer(rospy.Duration().from_sec(1.0/20), self.tfTimerCallback) 
        #rospy.Timer(rospy.Duration.from_sec(run_timeout), self.run_timeout_callback)
        #rospy.Timer(rospy.Duration.from_sec(ps_snapshot_period), self.ps_snapshot_timer_callback) 

        #tf send
        self.broadcaster = tf2_ros.TransformBroadcaster()
        self.transformStamped = geometry_msgs.msg.TransformStamped()

        #tf configuration 
        self.transformStamped.header.frame_id = "map"
        self.transformStamped.child_frame_id = "base_footprint"
        self.transformStamped.transform.translation.x = self.initial_pose.pose.pose.position.x   
        self.transformStamped.transform.translation.y = self.initial_pose.pose.pose.position.y  
        self.transformStamped.transform.translation.z = self.initial_pose.pose.pose.position.z  
        self.transformStamped.transform.rotation.x = self.initial_pose.pose.pose.orientation.x 
        self.transformStamped.transform.rotation.y = self.initial_pose.pose.pose.orientation.y   
        self.transformStamped.transform.rotation.z = self.initial_pose.pose.pose.orientation.z   
        self.transformStamped.transform.rotation.w = self.initial_pose.pose.pose.orientation.w   
        print("TF transform OK")


        # setup service clients    #we will not use any service so maybe It is not use full 
        #self.pause_physics_service_client = rospy.ServiceProxy(pause_physics_service, Empty)
        #self.unpause_physics_service_client = rospy.ServiceProxy(unpause_physics_service, Empty)
        #self.set_entity_state_service_client = rospy.ServiceProxy(set_entity_state_service, SetModelState)
        #self.global_localization_service_client = rospy.ServiceProxy(global_localization_service, Empty)


        # setup buffers   #we dont need it because we are publishing in tf area with tf broadcaster. 
        #self.tf_buffer = tf2_ros.Buffer()
        #self.tf_listener = tf2_ros.TransformListener(self.tf_buffer)


        # setup publishers
        self.traversal_path_publisher = rospy.Publisher("~/traversal_path", Path, latch=True, queue_size=1)
        #self.initial_pose_publisher = rospy.Publisher(initial_pose_topic, PoseWithCovarianceStamped, queue_size=1)


        # setup subscribers
        self.path_subscriber = rospy.Subscriber('/move_base/NavfnROS/plan', Path, self.pathCallback)
        self.path_subscriber = rospy.Subscriber('/move_base/GlobalPlanner/plan', Path, self.pathCallback)
        #you can add your subscribers here
        #you can add subscriber path here
        

        # setup action clients
        self.navigate_to_pose_action_client = SimpleActionClient(navigate_to_pose_action, MoveBaseAction)
        # self.navigate_to_pose_action_goal_future = None
        # self.navigate_to_pose_action_result_future = None


    def voronoi_graph_node_finder(self):
        print_info("Entered -> deleaved reduced Voronoi graph from ground truth map")

        # get deleaved reduced Voronoi graph from ground truth map)
        #(in here we are taking a list from voronoi graph we will use later it
        #(but right now we can add only one goal here then we can add volonoi graph after one goal achieved)
        voronoi_graph = self.ground_truth_map.deleaved_reduced_voronoi_graph(minimum_radius=2*self.robot_radius).copy()
        minimum_length_paths = nx.all_pairs_dijkstra_path(voronoi_graph, weight='voronoi_path_distance')
        minimum_length_costs = dict(nx.all_pairs_dijkstra_path_length(voronoi_graph, weight='voronoi_path_distance'))
        costs = defaultdict(dict)

        for i, paths_dict in minimum_length_paths:
            for j in paths_dict.keys():
                if i != j:
                    costs[i][j] = minimum_length_costs[i][j] 

        # in case the graph has multiple unconnected components, remove the components with less than two nodes
        too_small_voronoi_graph_components = list(filter(lambda component: len(component) < 2, nx.connected_components(voronoi_graph)))

        for graph_component in too_small_voronoi_graph_components:
            voronoi_graph.remove_nodes_from(graph_component)

        if len(voronoi_graph.nodes) < 2:
            self.write_event('insufficient_number_of_nodes_in_deleaved_reduced_voronoi_graph')
            raise RunFailException("insufficient number of nodes in deleaved_reduced_voronoi_graph, can not generate traversal path")

        # get greedy path traversing the whole graph starting from a random node
        traversal_path_indices = list()
        current_node = random.choice(list(voronoi_graph.nodes))
        nodes_queue = set(nx.node_connected_component(voronoi_graph, current_node))
        while len(nodes_queue):
            candidates = list(filter(lambda node_cost: node_cost[0] in nodes_queue, costs[current_node].items()))
            candidate_nodes, candidate_costs = zip(*candidates)
            next_node = candidate_nodes[int(np.argmin(candidate_costs))]
            traversal_path_indices.append(next_node)
            current_node = next_node
            nodes_queue.remove(next_node)

        # convert path of nodes to list of poses (as deque so they can be popped)
        self.traversal_path_poses = deque()
        for node_index in traversal_path_indices:
            pose = Pose()
            pose.position.x, pose.position.y = voronoi_graph.nodes[node_index]['vertex']
            q = pyquaternion.Quaternion(axis=[0, 0, 1], radians=np.random.uniform(-np.pi, np.pi))
            pose.orientation = Quaternion(w=q.w, x=q.x, y=q.y, z=q.z)
            self.traversal_path_poses.append(pose)

        # publish the traversal path for visualization
        self.traversal_path_msg = Path()
        self.traversal_path_msg.header.frame_id = self.fixed_frame
        self.traversal_path_msg.header.stamp = rospy.Time.now()
        for traversal_pose in self.traversal_path_poses:
            traversal_pose_stamped = PoseStamped()
            traversal_pose_stamped.header = self.traversal_path_msg.header
            traversal_pose_stamped.pose = traversal_pose
            self.traversal_path_msg.poses.append(traversal_pose_stamped)

        # pop the first pose from traversal_path_poses and set it as initial pose
        self.initial_pose = PoseWithCovarianceStamped()
        self.initial_pose.header.frame_id = self.fixed_frame
        self.initial_pose.header.stamp = rospy.Time.now()
        self.initial_pose.pose.pose = self.traversal_path_poses.popleft()
        #self.initial_pose.pose.covariance = list(self.initial_pose_covariance_matrix.flat)
        print("INITIAL POINT READY")
        #print("init pose:", self.initial_pose)


    def pathCallback(self,pathMessage):
        #print("sendign path message ", pathMessage)
        print("Path message recieved")
        self.path_recieve = True


    def start_run(self):
        print_info("preparing to start run")
        
        self.traversal_path_publisher.publish(self.traversal_path_msg)  #traversal path publisher for visualization

        self.num_goals = len(self.traversal_path_poses)

        self.write_event('run_start')
        #self.run_started = True

        # send goals
        
        """for k in range(self.num_goals): #in traversal_path_poses ve have all voronoi graph points and we are sending goal for each ontraversal_path_msge. in my project first I need to send one goal or farthest one

            print_info("goal {} / {}".format(self.goal_sent_count + 1, self.num_goals))

            if not self.navigate_to_pose_action_client.wait_for_server(timeout=rospy.Duration.from_sec(5.0)):
                self.write_event('failed_to_communicate_with_navigation_node')
                raise RunFailException("navigate_to_pose action server not available")

            if len(self.traversal_path_poses) == 0:
                self.write_event('insufficient_number_of_poses_in_traversal_path')
                raise RunFailException("insufficient number of poses in traversal path, can not send goal")

            goal_msg = MoveBaseGoal()
            goal_msg.target_pose.header.stamp = rospy.Time.now()
            goal_msg.target_pose.header.frame_id = self.fixed_frame
            goal_msg.target_pose.pose = self.traversal_path_poses.popleft()
            self.current_goal = goal_msg

            self.navigate_to_pose_action_client.send_goal(goal_msg)
            self.write_event('target_pose_set')
            self.goal_sent_count += 1
            print("sending goal")

            self.current_goal = None
        
        self.write_event('run_completed')
        rospy.signal_shutdown("run_completed")"""

        #only one random goal node send ############################################## 

        if not self.navigate_to_pose_action_client.wait_for_server(timeout=rospy.Duration.from_sec(5.0)):
            self.write_event('failed_to_communicate_with_navigation_node')
            raise RunFailException("navigate_to_pose action server not available")
        
        randomGoalPose = random.choice(self.traversal_path_poses)
        goal_msg = MoveBaseGoal()
        goal_msg.target_pose.header.stamp = rospy.Time.now()
        goal_msg.target_pose.header.frame_id = self.fixed_frame
        goal_msg.target_pose.pose =  randomGoalPose
        #print("GOAL :", goal_msg)
        
        self.navigate_to_pose_action_client.send_goal(goal_msg)
        self.write_event('target_pose_set')
        print("sending goal")

        self.write_event('run_completed')
        rospy.signal_shutdown("run_completed")
        #only one random goal node send end ##############################################

    def tfTimerCallback(self,event):
        self.transformStamped.header.stamp = rospy.Time.now()
        self.broadcaster.sendTransform(self.transformStamped)
        #print(event)    

    def ros_shutdown_callback(self):
        """
        This function is called when the node receives an interrupt signal (KeyboardInterrupt).
        """
        print_info("asked to shutdown, terminating run")
        self.write_event('ros_shutdown')
        self.write_event('supervisor_finished')

    def end_run(self):
        """
        This function is called after the run has completed, whether the run finished correctly, or there was an exception.
        The only case in which this function is not called is if an exception was raised from self.__init__
        """
        #self.estimated_poses_df.to_csv(self.estimated_poses_file_path, index=False)
        #self.estimated_correction_poses_df.to_csv(self.estimated_correction_poses_file_path, index=False)
        self.ground_truth_poses_df.to_csv(self.ground_truth_poses_file_path, index=False)
        # TODO amcl num_particles

    def run_timeout_callback(self, _):
        print_error("terminating supervisor due to timeout, terminating run")
        self.write_event('run_timeout')
        self.write_event('supervisor_finished')
        raise RunFailException("run_timeout")

   
    def ps_snapshot_timer_callback(self, _):
        ps_snapshot_file_path = path.join(self.ps_output_folder, "ps_{i:08d}.pkl".format(i=self.ps_snapshot_count))

        processes_dicts_list = list()
        for process in self.ps_processes:
            try:
                process_copy = copy.deepcopy(process.as_dict())  # get all information about the process
            except psutil.NoSuchProcess:  # processes may have died, causing this exception to be raised from psutil.Process.as_dict
                continue
            try:
                # delete uninteresting values
                del process_copy['connections']
                del process_copy['memory_maps']
                del process_copy['environ']

                processes_dicts_list.append(process_copy)
            except KeyError:
                pass
        try:
            with open(ps_snapshot_file_path, 'wb') as ps_snapshot_file:
                pickle.dump(processes_dicts_list, ps_snapshot_file)
        except TypeError:
            print_error(traceback.format_exc())

        self.ps_snapshot_count += 1

    def init_run_events_file(self):
        backup_file_if_exists(self.run_events_file_path)
        try:
            with open(self.run_events_file_path, 'w') as run_events_file:
                run_events_file.write("{t}, {event}\n".format(t='timestamp', event='event'))
        except IOError:
            rospy.logerr("slam_benchmark_supervisor.init_event_file: could not write header to run_events_file")
            rospy.logerr(traceback.format_exc())

    def write_event(self, event):
        t = rospy.Time.now().to_sec()
        print_info("t: {t}, event: {event}".format(t=t, event=str(event)))
        try:
            with open(self.run_events_file_path, 'a') as run_events_file:
                run_events_file.write("{t}, {event}\n".format(t=t, event=str(event)))
        except IOError:
            rospy.logerr("slam_benchmark_supervisor.write_event: could not write event to run_events_file: {t} {event}".format(t=t, event=str(event)))
            rospy.logerr(traceback.format_exc())