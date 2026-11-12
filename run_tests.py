#!/usr/bin/env python3
"""Run the whole test suite with one command: python3 run_tests.py"""
import os
import sys
import unittest

if __name__ == "__main__":
    here = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, here)
    suite = unittest.TestLoader().discover(os.path.join(here, "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
