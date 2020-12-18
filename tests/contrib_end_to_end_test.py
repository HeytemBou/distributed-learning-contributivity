# -*- coding: utf-8 -*-
"""
This enables to parameterize end to end tests - the tests are run by Travis each time you commit to the github repo
"""

from mplc.experiment import Experiment
from mplc.scenario import Scenario
from mplc.experiment import init_experiment_from_config_file

from . import test_utils


class Test_EndToEndTest:

    def test_titanic_contrib(self):
        """
        Test contributivity score on titanic dataset
        """

        titanic_scenario = Scenario(2, [0.1, 0.9], epoch_count=3, minibatch_count=1, dataset_name='titanic',
                                    contributivity_methods=["Federated SBS linear", "Shapley values"])
        exp = Experiment(experiment_name='end_to_end_test_contrib_titanic', nb_repeat=1,
                         scenarios_list=[titanic_scenario])
        exp.run()

        df = test_utils.get_latest_dataframe("*end_to_end_test*")

        # Two contributivity methods for each partner --> 4 lines
        assert len(df) == 4

        for contributivity_method in df.contributivity_method.unique():

            current_df = df[df.contributivity_method == contributivity_method]

            small_dataset_score = current_df.loc[current_df.dataset_fraction_of_partner == 0.1, "contributivity_score"]
            big_dataset_score = current_df.loc[current_df.dataset_fraction_of_partner == 0.9, "contributivity_score"]

            assert small_dataset_score.values < big_dataset_score.values

    def test_mnist_contrib(self):
        """
        Test contributivity score on mnist dataset
        """

        # run test from config file
        experiment = init_experiment_from_config_file("tests/config_end_to_end_test_contrib.yml")
        experiment.run()

        df = test_utils.get_latest_dataframe("*end_to_end_test*")

        # Three contributivity methods for each partner --> 6 lines
        assert len(df) == 6

        # Every contributivity estimate should be between -1 and 1
        assert df.contributivity_score.max() < 1
        assert df.contributivity_score.min() > -1

        for contributivity_method in df.contributivity_method.unique():

            current_df = df[df.contributivity_method == contributivity_method]

            small_dataset_score = current_df.loc[current_df.dataset_fraction_of_partner == 0.1, "contributivity_score"]
            big_dataset_score = current_df.loc[current_df.dataset_fraction_of_partner == 0.9, "contributivity_score"]

            assert small_dataset_score.values < big_dataset_score.values
