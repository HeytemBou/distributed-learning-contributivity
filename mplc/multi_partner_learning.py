# -*- coding: utf-8 -*-
"""
Functions for model training and evaluation (single-partner and multi-partner cases)
"""

import operator
import os
import pickle
from abc import ABC, abstractmethod
from timeit import default_timer as timer

import matplotlib.pyplot as plt
import numpy as np
from keras.backend.tensorflow_backend import clear_session
from keras.callbacks import EarlyStopping
from loguru import logger
from sklearn.preprocessing import normalize

from . import constants


class MultiPartnerLearning(ABC):

    def __init__(self,
                 partners_list,
                 epoch_count,
                 minibatch_count,
                 dataset,
                 aggregation_weighting="uniform",
                 is_early_stopping=True,
                 is_save_data=False,
                 save_folder="",
                 init_model_from="random_initialization",
                 use_saved_weights=False,
                 ):

        # Attributes related to partners
        self.partners_list = partners_list

        # Attributes related to the data and the model
        self.val_data = (dataset.x_val, dataset.y_val)
        self.test_data = (dataset.x_test, dataset.y_test)
        self.dataset_name = dataset.name
        self.generate_new_model = dataset.generate_new_model
        self.init_model_from = init_model_from
        self.use_saved_weights = use_saved_weights
        # Initialize the model
        if self.use_saved_weights:
            logger.info("Init model with previous coalition model")
        else:
            logger.info("Init new model")
        self.model = self.init_with_model()

        # Attributes related to the multi-partner learning approach
        self.aggregation_weighting = aggregation_weighting

        # Attributes related to iterating at different levels
        self.epoch_count = epoch_count
        self.epoch_index = 0
        self.minibatch_count = minibatch_count
        self.minibatch_index = 0
        self.is_early_stopping = is_early_stopping

        # Attributes for storing intermediate artefacts and results
        self.minibatched_x_train = [None] * self.partners_count
        self.minibatched_y_train = [None] * self.partners_count
        self.aggregation_weights = []
        self.models_weights_list = [None] * self.partners_count
        self.scores_last_learning_round = [None] * self.partners_count
        self.score_matrix_per_partner = np.nan * np.zeros(
            shape=(self.epoch_count, self.minibatch_count, self.partners_count))
        self.score_matrix_collective_models = np.nan * np.zeros(shape=(self.epoch_count, self.minibatch_count + 1))
        self.loss_collective_models = []
        self.test_score = None
        self.nb_epochs_done = int
        self.is_save_data = is_save_data
        self.save_folder = save_folder
        self.learning_computation_time = None

        logger.debug("MultiPartnerLearning object instantiated.")

    @property
    def partners_count(self):
        return len(self.partners_list)

    def save_final_model_weights(self, model_to_save):
        """Save final model weights"""

        model_folder = os.path.join(self.save_folder, 'model')

        if not os.path.isdir(model_folder):
            os.makedirs(model_folder)

        model_to_save.save_weights(os.path.join(model_folder, self.dataset_name + '_final_weights.h5'))

        model_weights = model_to_save.get_weights()
        np.save(os.path.join(model_folder, self.dataset_name + '_final_weights.npy'), model_weights)

    def save_data(self):
        """Save figures, losses and metrics to disk"""

        history_data = {
            "loss_collective_models": self.loss_collective_models,
            "score_matrix_per_partner": self.score_matrix_per_partner,
            "score_matrix_collective_models": self.score_matrix_collective_models,
        }
        with open(self.save_folder / "history_data.p", 'wb') as f:
            pickle.dump(history_data, f)

        if not os.path.exists(self.save_folder / 'graphs/'):
            os.makedirs(self.save_folder / 'graphs/')
        plt.figure()
        plt.plot(self.loss_collective_models)
        plt.ylabel("Loss")
        plt.xlabel("Epoch")
        plt.savefig(self.save_folder / "graphs/federated_training_loss.png")
        plt.close()

        plt.figure()
        plt.plot(self.score_matrix_collective_models[: self.epoch_index + 1, self.minibatch_count])
        plt.ylabel("Accuracy")
        plt.xlabel("Epoch")
        # plt.yscale('log')
        plt.ylim([0, 1])
        plt.savefig(self.save_folder / "graphs/federated_training_acc.png")
        plt.close()

        plt.figure()
        plt.plot(self.score_matrix_per_partner[: self.epoch_index + 1, self.minibatch_count - 1, ])
        plt.title("Model accuracy")
        plt.ylabel("Accuracy")
        plt.xlabel("Epoch")
        plt.legend(["partner " + str(i) for i in range(self.partners_count)])
        # plt.yscale('log')
        plt.ylim([0, 1])
        plt.savefig(self.save_folder / "graphs/all_partners.png")
        plt.close()

    def split_in_minibatches(self):
        """Split the dataset passed as argument in mini-batches"""

        # Create the indices where to split
        split_indices = np.arange(1, self.minibatch_count + 1) / self.minibatch_count

        # Iterate over all partners and split their datasets
        for partner_index, partner in enumerate(self.partners_list):
            # Shuffle the dataset
            idx = np.random.permutation(len(partner.x_train))
            x_train, y_train = partner.x_train[idx], partner.y_train[idx]

            # Split the samples and labels
            self.minibatched_x_train[partner_index] = np.split(x_train, (split_indices[:-1] * len(x_train)).astype(int))
            self.minibatched_y_train[partner_index] = np.split(y_train, (split_indices[:-1] * len(y_train)).astype(int))

    def prepare_aggregation_weights(self):  # we can maybe think about a aggregator object
        """Returns a list of weights for the weighted average aggregation of model weights"""

        if self.aggregation_weighting == "uniform":
            self.aggregation_weights = [1 / self.partners_count] * self.partners_count
        elif self.aggregation_weighting == "data_volume":
            partners_sizes = [len(partner.x_train) for partner in self.partners_list]
            self.aggregation_weights = partners_sizes / np.sum(partners_sizes)
        elif self.aggregation_weighting == "local_score":
            self.aggregation_weights = self.scores_last_learning_round / np.sum(self.scores_last_learning_round)
        else:
            raise NameError("The aggregation_weighting value [" + self.aggregation_weighting + "] is not recognized.")

    def aggregate_model_weights(self):
        """Aggregate model weights from the list of models, with a weighted average"""

        weights_per_layer = list(zip(*self.models_weights_list))
        new_weights = list()

        for weights_for_layer in weights_per_layer:
            avg_weights_for_layer = np.average(
                np.array(weights_for_layer), axis=0, weights=self.aggregation_weights
            )
            new_weights.append(list(avg_weights_for_layer))

        return new_weights

    def save_model_for_partner(self, model, partner_index):
        """save a model with weight"""
        self.models_weights_list[partner_index] = model.get_weights()

    def build_model_from_weights(self, new_weights):
        """Generate a new model initialized with weights passed as arguments"""
        new_model = self.generate_new_model()
        new_model.set_weights(new_weights)
        return new_model

    def init_with_models(self):
        """Return a list of newly generated models, one per partner"""

        # Init a list to receive a new model for each partner
        partners_model_list = []

        # Generate a new model and add it to the list
        new_model = self.generate_new_model()
        if self.use_saved_weights:
            new_model.load_weights(self.init_model_from)

        # For each partner, create a new model and add it to the list

        partners_model_list.append(new_model)

        # For each remaining partner, duplicate the new model and add it to the list
        new_model_weights = new_model.get_weights()
        for i in range(len(self.partners_list) - 1):
            partners_model_list.append(self.build_model_from_weights(new_model_weights))

        return partners_model_list

    def init_with_model(self):
        new_model = self.generate_new_model()

        if self.use_saved_weights:
            new_model.load_weights(self.init_model_from)

        return new_model

    def init_with_agg_model(self):
        """Return a new model aggregating models from model_list"""

        self.prepare_aggregation_weights()
        return self.build_model_from_weights(self.aggregate_model_weights())

    def init_with_agg_models(self):
        """Return a list with the aggregated model duplicated for each partner"""

        self.prepare_aggregation_weights()
        partners_model_list = []
        for _ in self.partners_list:
            partners_model_list.append(self.build_model_from_weights(self.aggregate_model_weights()))
        return partners_model_list

    def log_collaborative_round_partner_result(self, partner, partner_index, validation_score):
        """Print the validation accuracy of the collaborative round"""

        epoch_nb_str = f"Epoch {str(self.epoch_index).zfill(2)}/{str(self.epoch_count - 1).zfill(2)}"
        mb_nb_str = f"Minibatch {str(self.minibatch_index).zfill(2)}/{str(self.minibatch_count - 1).zfill(2)}"
        partner_id_str = f"Partner id #{partner.id} ({partner_index}/{self.partners_count - 1})"
        val_acc_str = f"{round(validation_score, 2)}"

        logger.debug(f"{epoch_nb_str} > {mb_nb_str} > {partner_id_str} > val_acc: {val_acc_str}")

    def update_iterative_results(self, partner_index, fit_history):
        """Update the results arrays with results from the collaboration round"""

        validation_score = fit_history.history["val_accuracy"][0]

        self.scores_last_learning_round[partner_index] = validation_score  # TO DO check if coherent

        # At the end of each mini-batch, for each partner, populate the score matrix per partner
        self.score_matrix_per_partner[self.epoch_index, self.minibatch_index, partner_index] = validation_score

    def early_stop(self):
        if self.is_early_stopping:
            # Early stopping parameters
            if (
                    self.epoch_index >= constants.PATIENCE
                    and self.loss_collective_models[-1] > self.loss_collective_models[-constants.PATIENCE]
            ):
                logger.debug("         -> Early stopping criteria are met, stopping here.")
                return True
            else:
                logger.debug("         -> Early stopping criteria are not met, continuing with training.")
        else:
            return False

    def fit(self):
        """Return the score on test data of a final aggregated model trained in a federated way on each partner"""

        start = timer()

        partners_list = self.partners_list
        partners_count = self.partners_count

        # Else, continue onto a federated learning procedure
        partners_list = sorted(partners_list, key=operator.attrgetter("id"))
        logger.info(
            f"## Training and evaluating model on partners with ids: {['#' + str(p.id) for p in partners_list]}")

        # Train model (iterate for each epoch and mini-batch)
        while self.epoch_index < self.epoch_count:

            self.fit_epoch()  # perform an epoch on the self.model

            model_evaluation_val_data = self.model.evaluate(self.val_data[0],
                                                            self.val_data[1],
                                                            batch_size=constants.DEFAULT_BATCH_SIZE,
                                                            verbose=0,
                                                            )

            current_val_loss = model_evaluation_val_data[0]
            current_val_metric = model_evaluation_val_data[1]

            self.score_matrix_collective_models[self.epoch_index, self.minibatch_count] = current_val_metric
            self.loss_collective_models.append(current_val_loss)

            logger.info(f"   Model evaluation at the end of the epoch: "
                        f"{['%.3f' % elem for elem in model_evaluation_val_data]}")

            logger.debug("      Checking if early stopping criteria are met:")
            if self.early_stop():
                break
            self.epoch_index += 1

        # After last epoch or if early stopping was triggered, evaluate model on the global testset
        logger.info("### Evaluating model on test data:")
        model_evaluation_test_data = self.model.evaluate(self.test_data[0],
                                                         self.test_data[1],
                                                         batch_size=constants.DEFAULT_BATCH_SIZE,
                                                         verbose=0,
                                                         )

        logger.info(f"   Model metrics names: {self.model.metrics_names}")
        logger.info(f"   Model metrics values: {['%.3f' % elem for elem in model_evaluation_test_data]}")
        self.test_score = model_evaluation_test_data[1]  # 0 is for the loss
        self.nb_epochs_done = self.epoch_index + 1

        # Plot training history
        if self.is_save_data:
            self.save_data()

        self.save_final_model_weights(self.model)

        logger.info("Training and evaluation on multiple partners: done.")
        end = timer()
        self.learning_computation_time = end - start

    @abstractmethod
    def fit_epoch(self):
        while self.minibatch_index < self.minibatch_count:
            self.fit_minibatch()
            self.minibatch_index += 1

    @abstractmethod
    def fit_minibatch(self):
        pass


class SinglePartnerLearning(MultiPartnerLearning):
    def __init__(self,
                 partner,
                 epoch_count,
                 minibatch_count,
                 dataset,
                 is_early_stopping=True,
                 is_save_data=False,
                 save_folder="",
                 init_model_from="random_initialization",
                 use_saved_weights=False, ):
        if self.partners_count > 1:
            raise ValueError('More than one partner is provided')
        super(SinglePartnerLearning, self).__init__([partner],
                                                    epoch_count,
                                                    minibatch_count,
                                                    dataset,
                                                    is_early_stopping=is_early_stopping,
                                                    is_save_data=is_save_data,
                                                    save_folder=save_folder,
                                                    init_model_from=init_model_from,
                                                    use_saved_weights=use_saved_weights,
                                                    aggregation_weighting='', )
        self.partner = partner

    def fit(self):
        """Return the score on test data of a model trained on a single partner"""

        start = timer()
        logger.info(f"## Training and evaluating model on partner with id #{self.partner.id}")

        # Set if early stopping if needed
        cb = []
        es = None
        if self.is_early_stopping:
            es = EarlyStopping(monitor='val_loss', mode='min', verbose=0, patience=constants.PATIENCE)
            cb.append(es)

        # Train model
        logger.info("   Training model...")
        history = self.model.fit(self.partner.x_train, self.partner.y_train, batch_size=self.partner.batch_size,
                                 epochs=self.epoch_count, verbose=0,
                                 validation_data=self.val_data)

        self.loss_collective_models.append(history.history["val_loss"][-1])
        # Evaluate trained model on test data
        model_evaluation_test_data = self.model.evaluate(self.test_data[0],
                                                         self.test_data[1],
                                                         batch_size=constants.DEFAULT_BATCH_SIZE,
                                                         verbose=0,
                                                         )
        logger.info(f"   Model evaluation on test data: "
                    f"{list(zip(self.model.metrics_names, ['%.3f' % elem for elem in model_evaluation_test_data]))}")

        # Save model score on test data
        self.test_score = model_evaluation_test_data[1]  # 0 is for the loss
        self.nb_epochs_done = (es.stopped_epoch + 1) if es.stopped_epoch != 0 else self.epoch_count

        end = timer()
        self.learning_computation_time = end - start

    def fit_epoch(self):
        pass

    def fit_minibatch(self):
        pass


class FederatedAverageLearning(MultiPartnerLearning):
    def __init__(self,
                 partners_list,
                 epoch_count,
                 minibatch_count,
                 dataset,
                 aggregation_weighting="uniform",
                 is_early_stopping=True,
                 is_save_data=False,
                 save_folder="",
                 init_model_from="random_initialization",
                 use_saved_weights=False,
                 ):
        # First, if only one partner, fall back to dedicated single partner function
        if self.partners_count == 1:
            raise ValueError('Only one partner is provided. Please use the dedicated SinglePartnerLearning class')
        super(FederatedAverageLearning, self).__init__(partners_list,
                                                       epoch_count,
                                                       minibatch_count,
                                                       dataset,
                                                       aggregation_weighting,
                                                       is_early_stopping,
                                                       is_save_data,
                                                       save_folder,
                                                       init_model_from,
                                                       use_saved_weights)

    def fit_epoch(self):
        # Clear Keras' old models
        clear_session()

        # Split the train dataset in mini-batches
        self.split_in_minibatches()

        # Iterate over mini-batches and train
        while self.minibatch_index < self.minibatch_count:
            self.fit_minibatch()
            self.minibatch_index += 1

            # At the end of each epoch,aggregate the models
        self.prepare_aggregation_weights()
        self.model = self.build_model_from_weights(self.aggregate_model_weights())

    def fit_minibatch(self):
        """Proceed to a collaborative round with a federated averaging approach"""

        logger.debug("Start new fedavg collaborative round ...")

        # Initialize variables
        is_very_first_minibatch = (self.epoch_index == 0 and self.minibatch_index == 0)

        # Starting model for each partner is the aggregated model from the previous mini-batch iteration
        if is_very_first_minibatch:  # Except for the very first mini-batch where it is a new model
            if self.use_saved_weights:
                logger.info(f"(fedavg) Very first minibatch of epoch n°{self.epoch_index}, "
                            f"init models with previous coalition model for each partner")
            else:
                logger.info(
                    f"(fedavg) Very first minibatch of epoch n°{self.epoch_index}, init new models for each partner")
            partners_model_list_for_iteration = self.init_with_models()
        else:
            logger.info(f"(fedavg) Minibatch n°{self.minibatch_index} of epoch n°{self.epoch_index}, "
                        f"init aggregated model for each partner with models from previous round")
            partners_model_list_for_iteration = self.init_with_agg_models()  # TODO change that by self.model.copy

        # Evaluate and store accuracy of mini-batch start model
        model_to_evaluate = partners_model_list_for_iteration[0]
        model_evaluation_val_data = model_to_evaluate.evaluate(self.val_data[0],
                                                               self.val_data[1],
                                                               batch_size=constants.DEFAULT_BATCH_SIZE,
                                                               verbose=0,
                                                               )
        self.score_matrix_collective_models[self.epoch_index, self.minibatch_index] = model_evaluation_val_data[1]

        # Iterate over partners for training each individual model
        for partner_index, partner in enumerate(self.partners_list):
            # Reference the partner's model
            partner_model = partners_model_list_for_iteration[partner_index]

            # Train on partner local data set
            history = partner_model.fit(self.minibatched_x_train[partner_index][self.minibatch_index],
                                        self.minibatched_y_train[partner_index][self.minibatch_index],
                                        batch_size=partner.batch_size,
                                        verbose=0,
                                        validation_data=self.val_data)

            # Log results of the round
            model_evaluation_val_data = history.history['val_accuracy'][0]
            self.log_collaborative_round_partner_result(partner, partner_index, model_evaluation_val_data)

            # Update the partner's model in the models' list
            self.save_model_for_partner(partner_model, partner_index)

            # Update iterative results
            self.update_iterative_results(partner_index, history)

        logger.debug("End of fedavg collaborative round.")


class SequentialLearning(MultiPartnerLearning):  # seq-pure
    def __init__(self,
                 partners_list,
                 epoch_count,
                 minibatch_count,
                 dataset,
                 aggregation_weighting="uniform",
                 is_early_stopping=True,
                 is_save_data=False,
                 save_folder="",
                 init_model_from="random_initialization",
                 use_saved_weights=False,
                 ):
        if self.partners_count == 1:
            raise ValueError('Only one partner is provided. Please use the dedicated SinglePartnerLearning class')
        super(SequentialLearning, self).__init__(partners_list,
                                                 epoch_count,
                                                 minibatch_count,
                                                 dataset,
                                                 aggregation_weighting,
                                                 is_early_stopping,
                                                 is_save_data,
                                                 save_folder,
                                                 init_model_from,
                                                 use_saved_weights)

    def fit_epoch(self):
        # Split the train dataset in mini-batches
        self.split_in_minibatches()

        # Iterate over mini-batches and train
        while self.minibatch_index < self.minibatch_count:
            self.fit_minibatch()
            self.minibatch_index += 1

    def fit_minibatch(self):
        """Proceed to a collaborative round with a sequential approach"""

        logger.debug("Start new sequential collaborative round ...")

        # Initialize variables
        is_last_round = self.minibatch_index == self.minibatch_count - 1

        # Evaluate and store accuracy of mini-batch start model
        model_evaluation_val_data = self.model.evaluate(self.val_data[0],
                                                        self.val_data[1],
                                                        batch_size=constants.DEFAULT_BATCH_SIZE,
                                                        verbose=0,
                                                        )
        self.score_matrix_collective_models[self.epoch_index, self.minibatch_index] = model_evaluation_val_data[1]

        # Iterate over partners for training the model sequentially
        shuffled_indexes = np.random.permutation(self.partners_count)
        logger.debug(f"(seq) Shuffled order for this sequential collaborative round: {shuffled_indexes}")
        for for_loop_idx, partner_index in enumerate(shuffled_indexes):

            partner = self.partners_list[partner_index]

            # Train on partner local data set
            history = self.model.fit(self.minibatched_x_train[partner_index][self.minibatch_index],
                                     self.minibatched_y_train[partner_index][self.minibatch_index],
                                     batch_size=partner.batch_size,
                                     verbose=0,
                                     validation_data=self.val_data)

            # Log results of the round
            model_evaluation_val_data = history.history['val_accuracy'][0]
            self.log_collaborative_round_partner_result(partner, for_loop_idx, model_evaluation_val_data)

            # On final collaborative round, save the partner's model in the models' list
            if is_last_round:
                self.save_model_for_partner(self.model, partner_index)

            # Update iterative results
            self.update_iterative_results(partner_index, history)

        logger.debug("End of sequential collaborative round.")


class SequentialWithFinalAggLearning(SequentialLearning):
    def __init__(self,
                 partners_list,
                 epoch_count,
                 minibatch_count,
                 dataset,
                 aggregation_weighting="uniform",
                 is_early_stopping=True,
                 is_save_data=False,
                 save_folder="",
                 init_model_from="random_initialization",
                 use_saved_weights=False,
                 ):
        super(SequentialWithFinalAggLearning, self).__init__(partners_list,
                                                             epoch_count,
                                                             minibatch_count,
                                                             dataset,
                                                             aggregation_weighting,
                                                             is_early_stopping,
                                                             is_save_data,
                                                             save_folder,
                                                             init_model_from,
                                                             use_saved_weights)

    def fit(self):
        super(SequentialWithFinalAggLearning, self).fit()
        self.prepare_aggregation_weights()
        self.model = self.build_model_from_weights(self.aggregate_model_weights())


class SequentialAverageLearning(MultiPartnerLearning):  # TODO maybe this class can inherit from federatedAveraged
    def __init__(self,
                 partners_list,
                 epoch_count,
                 minibatch_count,
                 dataset,
                 aggregation_weighting="uniform",
                 is_early_stopping=True,
                 is_save_data=False,
                 save_folder="",
                 init_model_from="random_initialization",
                 use_saved_weights=False,
                 ):
        if self.partners_count == 1:
            raise ValueError('Only one partner is provided. Please use the dedicated SinglePartnerLearning class')
        super(SequentialAverageLearning, self).__init__(partners_list,
                                                        epoch_count,
                                                        minibatch_count,
                                                        dataset,
                                                        aggregation_weighting,
                                                        is_early_stopping,
                                                        is_save_data,
                                                        save_folder,
                                                        init_model_from,
                                                        use_saved_weights)

    def fit_epoch(self):
        # Split the train dataset in mini-batches
        self.split_in_minibatches()

        # Iterate over mini-batches and train
        while self.minibatch_index < self.minibatch_count:
            self.fit_minibatch()
            self.minibatch_index += 1

        # At the end of each epoch,aggregate the models
        self.prepare_aggregation_weights()
        self.model = self.build_model_from_weights(self.aggregate_model_weights())

    def fit_minibatch(self):
        """Proceed to a collaborative round with a sequential averaging approach"""

        logger.debug("Start new seqavg collaborative round ...")

        # Initialize variables
        is_very_first_minibatch = (self.epoch_index == 0 and self.minibatch_index == 0)

        # Starting model for each partner is the aggregated model from the previous collaborative round
        if is_very_first_minibatch:  # Except for the very first mini-batch where it is a new model
            if self.use_saved_weights:
                logger.info(f"(seqavg) Very first minibatch of epoch n°{self.epoch_index}, "
                            f"init model with previous coalition model for each partner")
            else:
                logger.info(
                    f"(seqavg) Very first minibatch of epoch n°{self.epoch_index}, init new model for each partner")
            model_for_round = self.init_with_model()
        else:
            logger.info(f"(seqavg) Minibatch n°{self.minibatch_index} of epoch n°{self.epoch_index}, "
                        f"init model by aggregating models from previous round")
            model_for_round = self.init_with_agg_model()

        # Evaluate and store accuracy of mini-batch start model
        model_evaluation_val_data = model_for_round.evaluate(self.val_data[0],
                                                             self.val_data[1],
                                                             batch_size=constants.DEFAULT_BATCH_SIZE,
                                                             verbose=0,
                                                             )
        self.score_matrix_collective_models[self.epoch_index, self.minibatch_index] = model_evaluation_val_data[1]

        # Iterate over partners for training each individual model
        shuffled_indexes = np.random.permutation(self.partners_count)
        logger.debug(f"(seqavg) Shuffled order for this seqavg collaborative round: {shuffled_indexes}")
        for for_loop_idx, partner_index in enumerate(shuffled_indexes):
            partner = self.partners_list[partner_index]

            # Train on partner local data set
            history = model_for_round.fit(self.minibatched_x_train[partner_index][self.minibatch_index],
                                          self.minibatched_y_train[partner_index][self.minibatch_index],
                                          batch_size=partner.batch_size,
                                          verbose=0,
                                          validation_data=self.val_data)

            # Log results
            model_evaluation_val_data = history.history['val_accuracy'][0]
            self.log_collaborative_round_partner_result(partner, for_loop_idx, model_evaluation_val_data)

            # Save the partner's model in the models' list
            self.save_model_for_partner(model_for_round, partner_index)

            # Update iterative results
            self.update_iterative_results(partner_index, history)

        logger.debug("End of seqavg collaborative round.")


def init_multi_partner_learning_from_scenario(scenario, is_save_data=True):
    mpl = scenario.multi_partner_learning_approach(
        scenario.partners_list,
        scenario.epoch_count,
        scenario.minibatch_count,
        scenario.dataset,
        scenario.aggregation_weighting,
        scenario.is_early_stopping,
        is_save_data,
        scenario.save_folder,
        scenario.init_model_from,
        scenario.use_saved_weights,
    )

    return mpl


class MplLabelFlip(MultiPartnerLearning):

    def __init__(self, scenario, is_save_data=False, epsilon=0.01):
        super().__init__(
            scenario.partners_list,
            scenario.epoch_count,
            scenario.minibatch_count,
            scenario.dataset,
            scenario.multi_partner_learning_approach,
            scenario.aggregation_weighting,
            scenario.is_early_stopping,
            is_save_data,
            scenario.save_folder,
            scenario.init_model_from,
            scenario.use_saved_weights,
        )

        self.epsilon = epsilon
        self.K = scenario.dataset.num_classes
        self.history_theta = [[None for _ in self.partners_list] for _ in range(self.epoch_count)]
        self.history_theta_ = [[None for _ in self.partners_list] for _ in range(self.epoch_count)]
        self.theta = [self.init_flip_proba() for _ in self.partners_list]
        self.theta_ = [None for _ in self.partners_list]

    def init_flip_proba(self):
        identity = np.identity(self.K)
        return identity * (1 - self.epsilon) + (1 - identity) * (self.epsilon / (self.K - 1))

    def fit(self):
        start = timer()
        logger.info(
            f"## Training and evaluating model on partners with ids: {['#' + str(p.id) for p in self.partners_list]}")

        logger.info("(LFlip) Very first minibatch, init new models for each partner")
        partners_models = self.init_with_models()

        self.epoch_index = 0
        while self.epoch_index < self.epoch_count:
            self.split_in_minibatches()
            self.minibatch_index = 0
            while self.minibatch_index < self.minibatch_count:
                logger.info(f"(LFLip) Minibatch n°{self.minibatch_index} of epoch n°{self.epoch_index}, "
                            f"init aggregated model for each partner with models from previous round")

                # Evaluate and store accuracy of mini-batch start model
                model_to_evaluate = partners_models[0]
                model_evaluation_val_data = self.evaluate_model(model_to_evaluate, self.val_data)
                self.score_matrix_collective_models[self.epoch_index,
                                                    self.minibatch_index] = model_evaluation_val_data[1]

                for partner_index, partner in enumerate(self.partners_list):
                    # Reference the partner's model
                    partner_model = partners_models[partner_index]
                    x_batch = self.minibatched_x_train[partner_index][self.minibatch_index]
                    y_batch = self.minibatched_y_train[partner_index][self.minibatch_index]

                    predictions = partner_model.predict(x_batch)
                    self.theta_[partner_index] = predictions  # Initialize the theta_

                    for idx, y in enumerate(y_batch):
                        self.theta_[partner_index][idx, :] *= self.theta[partner_index][:, np.argmax(y)]
                    self.theta_[partner_index] = normalize(self.theta_[partner_index], axis=0, norm='l1')
                    self.history_theta_[self.epoch_index][partner_index] = self.theta_[partner_index]

                    self.theta[partner_index] = self.theta_[partner_index].T.dot(y_batch)
                    self.theta[partner_index] = normalize(self.theta[partner_index], axis=1, norm='l1')
                    self.history_theta[self.epoch_index][partner_index] = self.theta[partner_index]

                    self.theta_[partner_index] = predictions
                    for idx, y in enumerate(y_batch):
                        self.theta_[partner_index][idx, :] *= self.theta[partner_index][:, np.argmax(y)]
                    self.theta_[partner_index] = normalize(self.theta_[partner_index], axis=0, norm='l1')

                    # draw of x_i
                    rand_idx = np.arange(len(x_batch))
                    # rand_idx =  np.random.randint(low=0, high=len(x_batch), size=(len(x_batch)))
                    flipped_minibatch_x_train = x_batch
                    flipped_minibatch_y_train = np.zeros(y_batch.shape)
                    for i, idx in enumerate(rand_idx):  # TODO vectorize
                        repartition = np.cumsum(
                            self.theta_[partner_index][idx, :])
                        a = np.random.random() - repartition  # draw
                        flipped_minibatch_y_train[i][np.argmin(np.where(a > 0, a, 0))] = 1
                        # not responsive to labels type.

                    train_data_for_fit_iteration = flipped_minibatch_x_train, flipped_minibatch_y_train

                    history = self.fit_model(
                        partner_model, train_data_for_fit_iteration, self.val_data, partner.batch_size)

                    # Log results of the round
                    model_evaluation_val_data = history.history['val_accuracy'][0]
                    self.log_collaborative_round_partner_result(partner, partner_index, model_evaluation_val_data)

                    # Update the partner's model in the models' list
                    self.save_model_for_partner(partner_model, partner_index)

                    # Update iterative results
                    self.update_iterative_results(partner_index, history)

                partners_models = self.init_with_agg_models()
                self.minibatch_index += 1

            # At the end of each epoch, evaluate the model for early stopping on a global validation set
            model_to_evaluate = partners_models[0]
            model_evaluation_val_data = self.evaluate_model(model_to_evaluate, self.val_data)

            current_val_loss = model_evaluation_val_data[0]
            current_val_metric = model_evaluation_val_data[1]

            self.score_matrix_collective_models[self.epoch_index, self.minibatch_count] = current_val_metric
            self.loss_collective_models.append(current_val_loss)

            logger.info(f"   Model evaluation at the end of the epoch: "
                        f"{['%.3f' % elem for elem in model_evaluation_val_data]}")

            self.epoch_index += 1

            logger.debug("      Checking if early stopping criteria are met:")
            if self.is_early_stopping:
                # Early stopping parameters
                if (
                        self.epoch_index >= constants.PATIENCE
                        and current_val_loss > self.loss_collective_models[-constants.PATIENCE]
                ):
                    logger.debug("         -> Early stopping criteria are met, stopping here.")
                    break
                else:
                    logger.debug("         -> Early stopping criteria are not met, continuing with training.")

        logger.info("### Evaluating model on test data:")
        model_evaluation_test_data = self.evaluate_model(model_to_evaluate, self.test_data)
        logger.info(f"   Model metrics names: {model_to_evaluate.metrics_names}")
        logger.info(f"   Model metrics values: {['%.3f' % elem for elem in model_evaluation_test_data]}")
        self.test_score = model_evaluation_test_data[1]  # 0 is for the loss
        self.nb_epochs_done = self.epoch_index

        # Plot training history
        if self.is_save_data:
            self.save_data()

        self.save_final_model_weights(model_to_evaluate)

        logger.info("Training and evaluation on multiple partners: done.")
        end = timer()
        self.learning_computation_time = end - start
