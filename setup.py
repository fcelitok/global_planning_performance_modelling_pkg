#!/usr/bin/env python

from distutils.core import setup
from catkin_pkg.python_setup import generate_distutils_setup

# for your packages to be recognized by python
d = generate_distutils_setup(
 packages=['global_planning_performance_modelling_ros'],
 package_dir={'global_planning_performance_modelling_ros': 'src/global_planning_performance_modelling_ros'}
)

setup(**d)
