# MIT License

# Copyright (c) 2024 The HuggingFace Team

# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""This module manages all the metrics occurring at the sample level. The results of said metrics are then aggregated
using simple function (min, mean, max, ...) at the corpus level. Most metrics fall under this category.
"""

import logging
import os
from typing import Callable, Literal, Union

import nltk
import numpy as np
from huggingface_hub import HfApi
from nltk.metrics.distance import edit_distance
from nltk.tokenize import word_tokenize
from nltk.tokenize.treebank import TreebankWordTokenizer
from nltk.translate.bleu_score import sentence_bleu
from pydantic import BaseModel
from scipy.stats import hypergeom
from transformers import AutoModelForSequenceClassification, AutoTokenizer

from lighteval.metrics.imports.bert_scorer import BERTScorer
from lighteval.metrics.imports.data_stats_metric import DataStatsMetric
from lighteval.metrics.imports.summac import SummaCZS
from lighteval.metrics.llm_as_judge import JudgeLM
from lighteval.metrics.normalizations import (
    LogProbNormalization,
    LogProbTokenNorm,
    normalize_log_probs,
    remove_braces,
    remove_braces_and_strip,
)
from lighteval.metrics.utils.judge_utils import get_judge_prompt_simpleqa, process_judge_response_simpleqa
from lighteval.models.model_output import ModelResponse
from lighteval.tasks.requests import Doc
from lighteval.utils.utils import as_list, safe_divide


logger = logging.getLogger(__name__)


class ExactMatches:
    def __init__(
        self,
        aggregation_function: Callable[[list[float]], float] = max,
        normalize_gold: Callable[[str], str] | None = None,
        normalize_pred: Callable[[str], str] | None = None,
        strip_strings: bool = False,
        type_exact_match: str = "full",
    ):
        """An exact match class.

        Args:
            aggregation_function (callable, optional): How to aggregate the item results. Defaults to max.
                Used if there are several golds or predictions on which scores were computed.
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
            strip_strings (bool, optional): Whether to strip both reference and predictions. Defaults to False.
            type_exact_match (str, optional): Defines what type of match to apply (post normalization if present).
                Can be any of `prefix`, `suffix` or `full`. Defaults to "full".
                `prefix` checks if the prediction starts with the gold,
                `suffix` if the prediction ends with the gold,
                `full` if the prediction and gold are equal
        """
        self.aggregation_function = aggregation_function
        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred
        self.strip_strings = strip_strings

        if type_exact_match not in ["prefix", "suffix", "full"]:
            # todo: we could add a set exact match
            raise ValueError(
                f"type_exact_match (used in parametrized_exact_match) must be one of prefix, suffix, or full. Was {type_exact_match} instead."
            )
        self.type_exact_match = type_exact_match

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> float:
        """Computes the metric over a list of golds and predictions for one single sample.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): Predicted strings

        Returns:
            float: Aggregated score over the current sample's items.
        """
        results = []
        # We might need to flatten golds if they are a list of lists
        golds = doc.get_golds()
        for gold in golds:
            for pred in model_response.text:
                results.append(self.compute_one_item(gold=gold, pred=pred))
        return self.aggregation_function(results)

    def compute_one_item(
        self,
        gold: str,
        pred: str,
    ) -> float:
        """Compares two strings only.

        Args:
            gold (str): One of the possible references
            pred (str): One of the possible predictions

        Returns:
            float: The exact match score. Will be 1 for a match, 0 otherwise.
        """
        if not pred:
            return 0

        if self.strip_strings:
            gold = gold.strip()
            pred = pred.strip()

        if self.normalize_gold:
            gold = self.normalize_gold(gold)
        if self.normalize_pred:
            pred = self.normalize_pred(pred)

        if self.type_exact_match == "prefix":
            return 1 if pred.startswith(gold) else 0
        if self.type_exact_match == "suffix":
            return 1 if pred.endswith(gold) else 0
        return 1 if gold == pred else 0


class F1_score:
    def __init__(
        self,
        aggregation_function: Callable[[list[float]], float] = max,
        normalize_gold: Callable[[str], str] | None = None,
        normalize_pred: Callable[[str], str] | None = None,
        strip_strings: bool = False,
    ):
        """An F1 score class. F1 is computed over the bag of words of the golds and predictions.

        Args:
            aggregation_function (callable, optional): How to aggregate the item results. Defaults to max.
                Used if there are several golds or predictions on which scores were computed.
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
            strip_strings (bool, optional): Whether to strip both reference and predictions. Defaults to False.
        """
        if aggregation_function is None:
            aggregation_function = max

        self.aggregation_function = aggregation_function
        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred
        self.strip_strings = strip_strings

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> float:
        """Computes the metric over a list of golds and predictions for one single sample.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): Predicted strings

        Returns:
            float: Aggregated score over the current sample's items.
        """
        results = []
        golds = doc.get_golds()
        predictions = model_response.text
        # We might need to flatten golds if they are a list of lists
        for gold in golds:
            for pred in predictions:
                results.append(self.compute_one_item(gold=gold, pred=pred))
        return self.aggregation_function(results)

    def compute_one_item(self, gold: str, pred: str) -> float:
        """Compares two strings only.

        Args:
            gold (str): One of the possible references
            pred (str): One of the possible predictions

        Returns:
            float: The f1 score over the bag of words, computed using nltk.
        """
        if self.normalize_gold:
            gold = self.normalize_gold(gold)

        if self.normalize_pred:
            pred = self.normalize_pred(pred)

        gold_bow = set(gold.split())
        pred_bow = set(pred.split())

        ret = nltk.scores.f_measure(gold_bow, pred_bow)

        if ret is None:
            return 0.0
        return ret


class LoglikelihoodAcc:
    def __init__(self, logprob_normalization: LogProbNormalization | None = None):
        """Log likelihood accuracy class. It tests if the highest log-probability of the possible choices
        is actually in the gold ones.

        Args:
            normalization (Normalization): The normalization to apply.
        """
        self.logprob_normalization = logprob_normalization

    # Solve the choices token lengths properly
    def compute(
        self,
        doc: Doc,
        model_response: ModelResponse,
        **kwargs,
    ) -> int:
        """Computes the log likelihood accuracy: is the choice with the highest logprob in `choices_logprob` present
        in the `gold_ixs`?

        Args:
            gold_ixs (list[int]): All the gold choices indices
            choices_logprob (list[float]): Summed log-probabilities of all the possible choices for the model, ordered as the choices.
            unconditioned_logprob (list[float] | None): Unconditioned log-probabilities for PMI normalization, ordered as the choices.
            choices_tokens (list[list[int]] | None): Tokenized choices for token normalization, ordered as the choices.
            formatted_doc (Doc): Original document for the sample.
                Used to get the original choices' length for possible normalization

        Returns:
            int: The eval score: 1 if the best log-prob choice is in gold, 0 otherwise.
        """
        n_choices = len(doc.choices)
        choices_logprobs = model_response.logprobs[:n_choices]
        unconditioned_logprobs = None

        if len(model_response.logprobs) == n_choices * 2:
            unconditioned_logprobs = model_response.logprobs[n_choices : n_choices * 2]

        gold_ixs = as_list(doc.gold_index)
        choices_tokens = model_response.output_tokens[:n_choices]

        normalized_log_probs = (
            normalize_log_probs(
                self.logprob_normalization,
                choices_logprobs,
                unconditioned_logprobs,
                doc.choices,
                choices_tokens,
            )
            if self.logprob_normalization
            else choices_logprobs
        )

        best_choice = np.argmax(normalized_log_probs)
        return int(best_choice in gold_ixs)


class NormalizedMultiChoiceProbability:
    def __init__(
        self,
        log_prob_normalization: LogProbNormalization | None = None,
        aggregation_function: Callable[[np.ndarray], float] = np.max,
    ):
        """Returns the probability of choosing the gold choice / (sum of probabilities of all choices). If multiple choices are gold,
        it returns the aggregated probability (default is max).

        Args:
            normalization (Normalization | None): The normalization to apply.
            aggregation_function (Callable[[list[float]], float]): The function to use to aggregate gold probabilities in case of multiple golds.
        """
        self.log_prob_normalization = log_prob_normalization
        self.aggregation_function = aggregation_function

    def compute(
        self,
        doc: Doc,
        model_response: ModelResponse,
        **kwargs,
    ) -> float:
        """Computes the log likelihood probability: chance of choosing the best choice.

        Args:
            gold_ixs (list[int]): All the gold choices indices
            choices_logprob (list[float]): Summed log-probabilities of all the possible choices for the model, ordered as the choices.
            unconditioned_logprob (list[float] | None): Unconditioned log-probabilities for PMI normalization, ordered as the choices.
            choices_tokens (list[list[int]] | None): Tokenized choices for token normalization, ordered as the choices.
            formatted_doc (Doc): Original document for the sample.
                Used to get the original choices' length for possible normalization

        Returns:
            float: The probability of the best log-prob choice being a gold choice.
        """
        n_choices = len(doc.choices)
        choices_logprobs = model_response.logprobs[:n_choices]
        unconditioned_logprobs = None

        if len(model_response.logprobs) == n_choices * 2:
            unconditioned_logprobs = model_response.logprobs[n_choices : n_choices * 2]

        gold_ixs = as_list(doc.gold_index)
        choices_tokens = model_response.output_tokens[:n_choices]

        normalized_log_probs = (
            normalize_log_probs(
                self.log_prob_normalization,
                choices_logprobs,
                unconditioned_logprobs,
                doc.choices,
                choices_tokens,
            )
            if self.log_prob_normalization
            else choices_logprobs
        )
        normalized_probs = np.exp(normalized_log_probs)

        normalized_probs = safe_divide(normalized_probs[gold_ixs], np.sum(normalized_probs))
        gold_idx_agg_prob = self.aggregation_function(normalized_probs)
        return gold_idx_agg_prob


class Probability:
    def __init__(
        self,
        normalization: LogProbTokenNorm | None = None,
        aggregation_function: Callable[[np.ndarray], float] = np.max,
    ):
        """Returns the probability of choosing gold choice ignoring probabilities of other choices. If multiple choices are gold,
        it returns the aggregated probability (default is max) of the gold choices.

        Args:
            normalization (Normalization | None): The normalization to apply. Only Token Normalization is supported as others don't make sense.
            aggregation_function (Callable[[list[float]], float]): The function to use to aggregate gold probabilities in case of multiple golds.
        """
        self.log_prob_normalization = normalization
        self.aggregation_function = aggregation_function

    def compute(
        self,
        doc: Doc,
        model_response: ModelResponse,
        **kwargs,
    ) -> float:
        """Computes the log likelihood probability: chance of choosing the best choice.

        Args:
            gold_ixs (list[int]): All the gold choices indices
            choices_logprob (list[float]): Summed log-probabilities of all the possible choices for the model, ordered as the choices.
            unconditioned_logprob (list[float] | None): Unconditioned log-probabilities for PMI normalization, ordered as the choices.
            choices_tokens (list[list[int]] | None): Tokenized choices for token normalization, ordered as the choices.
            reference_texts (list[str] | None): Reference texts for token normalization, ordered as the choices.

        Returns:
            float: The probability of the best log-prob choice being a gold choice.
        """
        choices_tokens = model_response.output_tokens
        logprobs = model_response.logprobs
        reference_texts = doc.choices

        normalized_log_probs = (
            normalize_log_probs(
                normalization=self.log_prob_normalization,
                choices_tokens=choices_tokens,
                choices_logprob=logprobs,
                choices_text=reference_texts,
                unconditioned_logprob=None,
            )
            if self.log_prob_normalization
            else logprobs
        )
        probs = np.exp(normalized_log_probs)
        return self.aggregation_function(probs)


class Recall:
    def __init__(self, at: int) -> None:
        """Recall metric class. It checks if the top `at` best choices include one of the golds or not.

        Args:
            at (int): Depth level of the recall.
                Recall at 1 is equivalent to a logprob accuracy without normalization.
        """
        self.recall_depth = at

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> int:
        """Computes the recall at the requested depth level: looks at the `n` best predicted choices (with the
        highest log probabilities) and see if there is an actual gold among them.

        Args:
            gold_ixs (list[int]): All the gold choices indices
            choices_logprob (list[float]): Summed log-probabilities of all the possible choices for the model, ordered as the choices.

        Returns:
            int: Score: 1 if one of the top level predicted choices was correct, 0 otherwise.
        """
        choices_logprobs = model_response.logprobs
        gold_ixs = as_list(doc.gold_index)

        if self.recall_depth == 1:
            return int(np.argmax(choices_logprobs) in gold_ixs)
        return int(any(ix in gold_ixs for ix in np.array(choices_logprobs).argsort()[::-1][: self.recall_depth]))


class MRR:
    def __init__(self, length_normalization: bool = False):
        """A mean reciprocal rank class.

        Args:
            length_normalization (bool, optional): Whether to use normalization on choice length when computing the best log-probabilities. Defaults to False.
        """
        self.length_normalization = length_normalization

    def compute(self, model_response: ModelResponse, doc: Doc, **kwargs) -> float:
        """Mean reciprocal rank. Measures the quality of a ranking of choices (ordered by correctness).

        Args:
            gold_ixs (list[int]): All the gold choices indices
            choices_logprob (list[float]): Summed log-probabilities of all the possible choices for the model, ordered as the choices.
            formatted_doc (Doc): Original document for the sample.
                Used to get the original choices' length for possible normalization

        Returns:
            float: MRR score.
        """
        choices_logprobs = model_response.logprobs
        gold_ixs = as_list(doc.gold_index)
        choices_logprobs = model_response.logprobs

        if self.length_normalization:
            choices_logprobs = [
                choice_logprob / len(choice) for choice_logprob, choice in zip(choices_logprobs, doc.choices)
            ]

        ranked_choices = [sorted(choices_logprobs, reverse=True).index(choices_logprobs[gold]) for gold in gold_ixs]
        return 1.0 / (min(ranked_choices) + 1)


def acc_golds_likelihood(doc, model_response, **kwargs) -> int:
    """Tests if at least one of predicted gold targets' argmax of logits equals the gold.

    Args:
        argmax_logits_eq_gold_list (list[int]): List of scores 1/0 indicating whether the argmax of logits equals the gold

    Returns:
        int: 1 if at least one of the possible golds has argmax of logits == gold, 0 otherwise
    """
    return int(any(model_response.argmax_logits_eq_gold))


class ROUGE:
    ALLOWED_ROUGE_METHODS = ["rouge1", "rouge2", "rougeL", "rougeLsum"]

    def __init__(
        self,
        methods: str | list[str],
        multiple_golds: bool = False,
        bootstrap: bool = False,
        normalize_gold: Callable | None = None,
        normalize_pred: Callable | None = None,
        aggregation_function: Callable | None = None,
        tokenizer: object = None,
    ):
        """A ROUGE wrapper method. Relies on `rouge_scorer`.

        Args:
            methods (str | list[str]): What type of ROUGE scoring to use. Can be one or any of `rouge1`, `rouge2`, `rougeL` or `rougeLsum`.
            multiple_golds (bool, optional): Whether to compute ROUGE by allowing the comparison to several golds
                at once, or to compute ROUGE on individual gold/prediction pairs and aggregate afterwards. Defaults to False.
            bootstrap (bool, optional): Whether to use bootstrapping. Defaults to False.
            aggregation_function (callable, optional): How to aggregate the item results. Defaults to max.
                Used if there are several golds or predictions on which scores were computed.
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
            tokenizer (object, optional): An object with `tokenize` method to be used by rouge scorer. If None, rouge-scorer's
                default tokenizer will be used.
        """
        if aggregation_function and bootstrap:
            logger.warning("Can't use both bootstrapping and an aggregation function in Rouge. Keeping bootstrap.")
        self.aggregation_function = aggregation_function
        if self.aggregation_function is None:
            self.aggregation_function = np.mean

        self.methods = as_list(methods)
        if any(method not in self.ALLOWED_ROUGE_METHODS for method in self.methods):
            raise ValueError(
                f"Rouge was initialised with method {methods}, which is not in {','.join(self.ALLOWED_ROUGE_METHODS)}"
            )
        self.multiple_golds = multiple_golds
        self.bootstrap = bootstrap
        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred
        self.tokenizer = tokenizer
        self.scorer = None

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> float | dict:
        """Computes the metric(s) over a list of golds and predictions for one single sample.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): Predicted strings

        Returns:
            float or dict: Aggregated score over the current sample's items.
                If several rouge functions have been selected, returns a dict which maps name and scores.
        """
        from rouge_score import rouge_scorer

        golds = doc.get_golds()
        predictions = model_response.text

        if self.scorer is None:
            self.scorer = rouge_scorer.RougeScorer(self.methods, tokenizer=self.tokenizer)

        # Normalize
        if self.normalize_gold:
            golds = [self.normalize_gold(g) for g in golds]

        if self.normalize_pred:
            predictions = [self.normalize_pred(p) for p in predictions]

        if self.bootstrap:  # For t5 style rouge score
            scores = self._rouge_score_with_bootsrap(golds=golds, predictions=predictions)
        elif self.multiple_golds:
            scores = self._rouge_score_multi_golds(golds=golds, preds=predictions)
        else:
            scores = self._rouge_score(golds=golds, preds=predictions)

        if len(scores) == 1:
            return list(scores.values())[0]
        return scores

    def _rouge_score(self, golds: list[str], preds: list[str]):
        scores = {m: [] for m in self.methods}
        for pred in preds:
            for gold in golds:
                cur_scores = self.scorer.score(gold, pred)
                for method in self.methods:
                    scores[method].append(cur_scores[method].fmeasure)
        return {method: self.aggregation_function(scores[method]) for method in self.methods}

    def _rouge_score_multi_golds(self, golds: list[str], preds: list[str]):
        scores = {m: [] for m in self.methods}
        for pred in preds:
            cur_scores = self.scorer.score_multi(golds, pred)
            for method in self.methods:
                scores[method].append(cur_scores[method].fmeasure)
        return {method: self.aggregation_function(scores[method]) for method in self.methods}

    def _rouge_score_with_bootsrap(self, golds: list[str], predictions: list[str]):
        from rouge_score import scoring

        aggregator = scoring.BootstrapAggregator()
        for g, p in zip(golds, predictions):
            aggregator.add_scores(self.scorer.score(g, p))
        result = aggregator.aggregate()
        return {method: result[method].mid.fmeasure * 100 for method in self.methods}


class BertScore:
    def __init__(
        self,
        normalize_gold: Callable | None = None,
        normalize_pred: Callable | None = None,
    ):
        r"""A BERT scorer class. Relies on some called extracted from `bert-score`. By default, will use the
        `microsoft/deberta-large-mnli` as scorer. For each tokenized (pred, target) pair, it computes Precision,
        Recall and F1 as following:

            Precision = \sum_{t=1}^{len(pred)} \div{max(Cos.Sim.(pred_t, target))}{IDF(pred_t)}

            Recall = \sum_{t=1}^{len(target)} \div{max(Cos.Sim.(target_t, pred))}{IDF(target_t)}

            F1 = \div{Precision * Recall}{Precision + Recall}

        in which `Cos.Sim.` is the Cosine Similarity metric and `IDF(.)` represents the Inverse Document
        Frequency of its input token. It defaults to 1 for all tokens and 0 for EOS and SEP tokens.

        Args:
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
        """
        self.bert_scorer = None

        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> dict[str, float]:
        """Computes the prediction, recall and f1 score using the bert scorer.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): Predicted strings

        Returns:
            dict: Scores over the current sample's items.
        """
        golds = doc.get_golds()
        predictions = model_response.text

        if self.bert_scorer is None:
            logger.warning("The first metric computation step might be a bit longer as we need to download the model.")
            # We only initialize on first compute
            self.bert_scorer = BERTScorer(
                model_type="microsoft/deberta-large-mnli", lang="en", rescale_with_baseline=True, num_layers=9
            )
        golds = as_list(golds)
        predictions = as_list(predictions)
        # Normalize
        if self.normalize_gold:
            golds = [self.normalize_gold(g) for g in golds]

        if self.normalize_pred:
            predictions = [self.normalize_pred(p) for p in predictions]

        p, r, f = self.bert_scorer.score(predictions, golds)
        return {"BERTScore-P": p[0].item(), "BERTScore-R": r[0].item(), "BERTScore-F": f[0].item()}


class Extractiveness:
    def __init__(
        self,
        normalize_input: callable = remove_braces,
        normalize_pred: callable = remove_braces_and_strip,
        input_column: str = "text",
    ):
        """
        Extractiveness metric class.

        Args:
            normalize_input (callable, optional): Function to normalize the input strings.
                Defaults to remove_braces from lighteval.metrics.normalizations if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to remove_braces_and_strip from lighteval.metrics.normalizations if no normalization is applied.
            input_column (str): Column in the formatted_doc to use for the input. Defaults to "text".
        """
        self.stats_metric = None
        self.normalize_input = normalize_input
        self.normalize_pred = normalize_pred
        self.input_column = input_column

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> dict[str, float]:
        """
        Compute the extractiveness of the predictions.

        This method calculates coverage, density, and compression scores for a single
        prediction against the input text.

        Args:
            predictions (list[str]): Predicted strings, a list of length 1.
            formatted_doc (Doc): The formatted document.

        Returns:
            dict[str, float]: The extractiveness scores.
        """
        if self.stats_metric is None:
            self.stats_metric = DataStatsMetric()

        inp = doc.specific[self.input_column]
        prediction = model_response.text[0]
        if self.normalize_input:
            inp = self.normalize_input(inp)
        if self.normalize_pred:
            prediction = self.normalize_pred(prediction)

        stats = self.stats_metric.evaluate_example(prediction, inp)
        return {
            "summarization_coverage": stats["coverage"],
            "summarization_density": stats["density"],
            "summarization_compression": stats["compression"],
        }


class Faithfulness:
    def __init__(
        self,
        normalize_input: Callable = remove_braces,
        normalize_pred: Callable = remove_braces_and_strip,
        input_column: str = "text",
    ):
        """
        Faithfulness metric class.

        Args:
            normalize_input (callable, optional): Function to normalize the input strings.
                Defaults to remove_braces from lighteval.metrics.normalizations if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to remove_braces_and_strip from lighteval.metrics.normalizations if no normalization is applied.
            input_column (str): Column in the formatted_doc to use for the input. Defaults to "text".
        """
        self.summac = None
        self.normalize_input = normalize_input
        self.normalize_pred = normalize_pred
        self.input_column = input_column

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> dict[str, float]:
        """
        Compute the faithfulness of the predictions.

        The SummaCZS (Summary Content Zero-Shot) model is used with configurable granularity and model variation.

        Args:
            predictions (list[str]): Predicted strings, a list of length 1.
            formatted_doc (Doc): The formatted document.

        Returns:
            dict[str, float]: The faithfulness scores.
        """
        if self.summac is None:
            self.summac = SummaCZS(
                granularity="sentence", model_name="vitc", imager_load_cache=False
            )  # , device=device)
        inp = doc.specific[self.input_column]
        predictions = model_response.text
        prediction = predictions[0]
        if self.normalize_input:
            inp = self.normalize_input(inp)
        if self.normalize_pred:
            prediction = self.normalize_pred(prediction)
        return self.summac.score_one(inp, prediction)["score"]


class BLEURT:
    def __init__(self):
        """Creates a BLEURT scorer using a light bleurt-tiny-512 model.
        For more complex use cases, could also be Elron/bleurt-base-128
        """
        self._tokenizer = None
        self._model = None

    @property
    def tokenizer(self):
        if self._tokenizer is None:
            self._tokenizer = AutoTokenizer.from_pretrained("Elron/bleurt-tiny-512")
        return self._tokenizer

    @property
    def model(self):
        if self._model is None:
            self._model = AutoModelForSequenceClassification.from_pretrained("Elron/bleurt-tiny-512")
            self._model.eval()
        return self._model

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> float:
        """Uses the stored BLEURT scorer to compute the score on the current sample.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): Predicted strings

        Returns:
            float: Score over the current sample's items.
        """
        predictions = model_response.text
        golds = doc.get_golds()
        if len(predictions) == 1:
            predictions = predictions * len(golds)
        scores = self.model(**self.tokenizer(golds, predictions, return_tensors="pt"))[0].squeeze()
        return scores.item()


class BLEU:
    def __init__(self, n_gram: int):
        """BLEU scorer class. Relies on `nltk`'s sentencebleu for scoring.
        TODO: Will have to move this to sacrebleu.

        Args:
            n_gram (int): Number of n_grams to use for scoring.
        """
        self.n_gram = n_gram

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs):
        """Computes the sentence level BLEU between the golds and each prediction, then takes the average.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): Predicted strings

        Returns:
            float: Score over the current sample's items.
        """
        golds = doc.get_golds()
        predictions = model_response.text
        return np.mean([self._bleu_score(golds, p) for p in predictions])

    def _bleu_score(self, gold: list[str], pred: str):
        """Computes the BLEU score between a list of golds and the current prediction.

        Args:
            golds (list[str]): Reference targets
            predictions (str): One of the predicted strings

        Returns:
            float: Score over the current prediction.
        """
        weights = [1 if ix == self.n_gram else 0 for ix in range(1, 5)]
        return sentence_bleu([word_tokenize(g) for g in gold], word_tokenize(pred), weights=weights)


class StringDistance:
    def __init__(
        self,
        metric_types: list[str] | str,
        strip_prediction: bool = True,
    ):
        """Contains a number of string distance and edition metrics. Relies on nltk to compute the edit distance.

        Args:
            metric_types (list[str] | str): Can be one or any of `longest_common_prefix_length`, `edit_distance` or `edit_similarity`.
            strip_prediction (bool, optional): Whether to strip the prediction. Defaults to True.
        """
        allowed_values = ["longest_common_prefix_length", "edit_distance", "edit_similarity"]
        metric_types = as_list(metric_types)
        if any(metric_type not in allowed_values for metric_type in metric_types):
            raise ValueError(
                f"{metric_types} is not a valid value for an EditDistance metric. Possible values are {','.join(allowed_values)}."
            )
        self.metric_types = metric_types
        self.strip_prediction = strip_prediction
        self.sample_aggregations = {"longest_common_prefix_length": max, "edit_distance": min, "edit_similarity": max}

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs):
        """Computes all the requested metrics on the golds and prediction.

        Args:
            golds (list[str]): A list of possible golds. If it contains more than one item, only the first one is kept.
            predictions (list[str]): Predicted strings.

        Returns:
           dict: The different scores computed
        """
        predictions = model_response.text
        golds = doc.get_golds()
        if len(golds) > 1:
            logger.warning(
                "Provided more than one gold to compute a string distance metric. Just using the first one."
            )
        reference = golds[0]

        result = {m: [] for m in self.metric_types}
        for sequence in predictions:
            if self.strip_prediction:
                completion = sequence.strip()
            else:
                completion = sequence

            # `reference` is the entire remaining book for each instance.
            # Truncate it here to be of the same length as the completion to ensure edit-distance is meaningful.
            truncated_reference = reference[: len(completion)]

            completion_tokens = np.array(TreebankWordTokenizer().tokenize(completion))
            truncated_reference_tokens = np.array(TreebankWordTokenizer().tokenize(truncated_reference))

            if "edit_distance" in self.metric_types:
                result["edit_distance"].append(edit_distance(s1=completion_tokens, s2=truncated_reference_tokens))
            if "edit_similarity" in self.metric_types:
                result["edit_similarity"].append(
                    self.edit_similarity(s1=completion_tokens, s2=truncated_reference_tokens)
                )
            if "longest_common_prefix_length" in self.metric_types:
                result["longest_common_prefix_length"].append(
                    self.longest_common_prefix_length(s1=completion_tokens, s2=truncated_reference_tokens)
                )

        final_result = {}
        # We cast to float as final results can be numpy types, not JSON serializable
        for m, v in result.items():
            final_result[m] = float(self.sample_aggregations[m](v))

        return final_result

    def longest_common_prefix_length(self, s1: np.ndarray, s2: np.ndarray) -> float:
        """Compute the length of the longest common prefix."""
        min_len = min(len(s1), len(s2))
        s1, s2 = s1[:min_len], s2[:min_len]
        (nonzeros,) = np.cumprod(s1 == s2).nonzero()
        return int(np.max(nonzeros)) + 1 if len(nonzeros) > 0 else 0

    def edit_similarity(self, s1, s2):
        """Compute the edit similarity between two lists of strings.

        Edit similarity is also used in the paper
            Lee, Katherine, et al.
            "Deduplicating training data makes language models better."
            arXiv preprint arXiv:2107.06499 (2021).
        """
        edist = edit_distance(s1, s2)
        return 1.0 - edist / max(len(s1), len(s2)) if len(s1) > 0 and len(s2) > 0 else 0


class JudgeLLM:
    available_models_openai = ["gpt-3.5-turbo", "gpt-4o", "gpt-4-turbo", "gpt-4", "gpt-4o-2024-08-06"]

    def __init__(
        self,
        judge_model_name: str,
        template: Callable,
        process_judge_response: Callable,
        judge_backend: Literal["litellm", "openai", "transformers", "vllm", "tgi", "inference-providers"],
        short_judge_name: str | None = None,
        response_format: BaseModel | None = None,
        url: str | None = None,
        hf_provider: str | None = None,
        max_tokens: int | None = None,
    ) -> None:
        logger.debug(f"Initializing JudgeLLM with backend: {judge_backend}, model: {judge_model_name}")

        api_key = None

        match judge_backend:
            case "openai":
                if judge_model_name not in self.available_models_openai:
                    raise ValueError(f"{judge_model_name} not in available models for llm as a judge metric")
                api_key = os.getenv("OPENAI_API_KEY")
                logger.debug("Using OpenAI backend for llm as a judge metric")

            case "tgi":
                api_key = os.getenv("HF_TOKEN")
                if url is None:
                    url = "https://api-inference.huggingface.co/v1/"
                logger.debug("Using TGI backend")

            case "inference-providers":
                api_key = os.getenv("HF_TOKEN")
                logger.debug("Using Hugging Face Inference backend")

            case "litellm":
                logger.debug("Using LiteLLM backend for llm as a judge metric")

            case "transformers" | "vllm":
                logger.debug("Checking availability of Transformers or VLLM model")
                api = HfApi()
                models = api.list_models(model_name=judge_model_name)
                if not models:
                    raise ValueError(f"{judge_model_name} not found on Hugging Face Hub")

            case _:
                raise ValueError(f"{judge_backend} is not a valid backend for llm as a judge metric")

        self.short_judge_name = short_judge_name
        self.judge = JudgeLM(
            model=judge_model_name,
            templates=template,
            process_judge_response=process_judge_response,
            judge_backend=judge_backend,
            response_format=response_format,
            api_key=api_key,
            url=url,
            hf_provider=hf_provider,
            max_tokens=max_tokens,
        )

    def compute(self, responses: list[ModelResponse], docs: list[Doc], **kwargs) -> list:
        raise NotImplementedError("This method should be implemented in the subclass.")


class JudgeLLMSimpleQA(JudgeLLM):
    def __init__(self):
        super().__init__(
            judge_model_name="gpt-4o-2024-08-06",
            template=get_judge_prompt_simpleqa,
            process_judge_response=process_judge_response_simpleqa,
            judge_backend="openai",
            short_judge_name="gpt4o",
        )

    def compute(self, responses: list[ModelResponse], docs: list[Doc], **kwargs) -> list:
        """
        Compute the score of a generative task using a llm as a judge.
        The generative task can be multiturn with 2 turns max, in that case, we
        return scores for turn 1 and 2. Also returns user_prompt and judgement
        which are ignored later by the aggregator.
        """
        questions = [formatted_doc.query for formatted_doc in docs]
        options = [formatted_doc.choices for formatted_doc in docs]
        golds = [formatted_doc.get_golds()[0] for formatted_doc in docs]
        predictions = [response.text[0] for response in responses]

        scores, messages, judgements = self.judge.evaluate_answer_batch(questions, predictions, options, golds)

        metrics = []
        for i in range(len(docs)):
            metrics.append(
                {
                    "simpleqa_judge": scores[i],
                    f"user_prompt_{self.short_judge_name}": messages[i],
                    f"judgement_{self.short_judge_name}": judgements[i],
                }
            )

        return metrics


class JudgeLLMMTBench(JudgeLLM):
    def compute(self, model_response: list[ModelResponse], docs: list[Doc], **kwargs):
        """
        Compute the score of a generative task using a llm as a judge.
        The generative task can be multiturn with 2 turns max, in that case, we
        return scores for turn 1 and 2. Also returns user_prompt and judgement
        which are ignored later by the aggregator.
        """
        import json

        # If we are evaluating a multiturn task, we need to have specific field in the formatted doc
        questions = [doc.specific["multi_turn_queries"] for doc in docs]
        golds = [doc.specific.get("reference", None) for doc in docs]
        predictions = [response.text[0] for response in model_response]

        query_context_1 = {"query": questions[0], "context": ""}
        query_context_2 = {"query": questions[1], "context": predictions[0]}

        score_turn_1, message_turn_1, judgement_turn_1 = self.judge.evaluate_answer(
            question=json.dumps(query_context_1, indent=2), answer=predictions[0], gold=golds[0] if golds else None
        )
        score_turn_2, message_turn_2, judgement_turn_2 = self.judge.evaluate_answer(
            question=json.dumps(query_context_2, indent=2), answer=predictions[1], gold=golds[1] if golds else None
        )

        return {
            "judge_score_turn_1": score_turn_1,
            "judge_score_turn_2": score_turn_2,
            "user_prompt": [message_turn_1, message_turn_2],
            "judgement": [judgement_turn_1, judgement_turn_2],
        }


class JudgeLLMMixEval(JudgeLLM):
    def compute(self, model_responses: list[ModelResponse], docs: list[Doc], **kwargs):
        """
        Compute the score of a generative task using a llm as a judge.
        The generative task can be multiturn with 2 turns max, in that case, we
        return scores for turn 1 and 2. Also returns user_prompt and judgement
        which are ignored later by the aggregator.
        """
        questions = [doc.specific["question"] for doc in docs]
        options = [doc.choices for doc in docs]
        golds = [doc.get_golds()[0] for doc in docs]
        predictions = [response.text[0] for response in model_responses]

        scores, messages, judgements = self.judge.evaluate_answer_batch(questions, predictions, options, golds)

        metrics = []
        for i in range(len(docs)):
            metrics.append(
                {
                    f"judge_score_{self.short_judge_name}": scores[i],
                    f"user_prompt_{self.short_judge_name}": messages[i],
                    f"judgement_{self.short_judge_name}": judgements[i],
                }
            )

        return metrics


class MajAtK:
    def __init__(
        self,
        k: int,
        normalize_gold: Callable | None = None,
        normalize_pred: Callable | None = None,
        strip_strings: bool = False,
        type_exact_match: str = "full",
    ):
        """An exact match class.

        Args:
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
            strip_strings (bool, optional): Whether to strip both reference and predictions. Defaults to False.
            type_exact_match (str, optional): Defines what type of match to apply (post normalization if present).
                Can be any of `prefix`, `suffix` or `full`. Defaults to "full".
                `prefix` checks if the prediction starts with the gold,
                `suffix` if the prediction ends with the gold,
                `full` if the prediction and gold are equal
        """
        self.k = k
        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred
        self.strip_strings = strip_strings

        if type_exact_match not in ["prefix", "suffix", "full"]:
            # todo: we could add a set exact match
            raise ValueError(
                f"type_exact_match (used in parametrized_exact_match) must be one of prefix, suffix, or full. Was {type_exact_match} instead."
            )
        self.type_exact_match = type_exact_match

    def compute(self, model_response: ModelResponse, docs: Doc, **kwargs):
        """Computes the metric over a list of golds and predictions for one single sample.
        It applies normalisation (if needed) to model prediction and gold, and takes the most frequent answer of all the available ones,
        then compares it to the gold.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): k predicted strings

        Returns:
            float: Aggregated score over the current sample's items.
        """
        golds = docs.get_golds()
        predictions = model_response.text
        if len(golds) > 1:
            raise Exception("Cannot compute maj@k with several golds")

        gold = self.get_processed_gold(golds[0])
        all_answers = []
        for pred in predictions[: self.k]:
            all_answers.append(self.get_processed_pred(pred=pred))
        majority_prediction = max(all_answers, key=all_answers.count)
        return self.compute_score(majority_prediction, gold)

    def get_processed_gold(self, gold: str) -> str:
        if self.strip_strings:
            gold = gold.strip()

        if self.normalize_gold:
            gold = self.normalize_gold(gold)

        return gold

    def get_processed_pred(self, pred: str) -> str:
        if not pred:
            return ""

        if self.strip_strings:
            pred = pred.strip()

        if self.normalize_pred:
            pred = self.normalize_pred(pred)

        return pred

    def compute_score(self, pred: str, gold: str) -> int:
        if self.type_exact_match == "prefix":
            return 1 if pred.startswith(gold) else 0
        if self.type_exact_match == "suffix":
            return 1 if pred.endswith(gold) else 0
        return 1 if gold == pred else 0


class PassAtK:
    def __init__(
        self,
        k: int,
        n: int | None = None,
        normalize_gold: Callable | None = None,
        normalize_pred: Callable | None = None,
        strip_strings: bool = False,
        sample_scoring_function: Callable[[Doc, ModelResponse], float] | str | None = None,
    ):
        """Computing pass at k

        Args:
            k (int): Threshold for the number of successful attempts.
            n (int): Number of samples to generate
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
            strip_strings (bool, optional): Whether to strip both reference and predictions. Defaults to False.
            sample_scoring_function (callable or str, optional): Function to use to score each sample.
                Either pass the full function (should take a string prediction and a string gold, and return a score between 0 and 1)
                a string (any of `prefix`, `suffix` or `full`) to define the type of exact match that you want, or nothing to defaults to "full".
                    `prefix` checks if the prediction starts with the gold,
                    `suffix` if the prediction ends with the gold,
                    `full` if the prediction and gold are equal
        """
        self.k = k
        self.n = n
        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred
        self.strip_strings = strip_strings

        # Managed the logic of the per prediction of sample scoring
        if callable(sample_scoring_function):
            self.score_sample = sample_scoring_function
            self.type_exact_match = None
        else:
            if isinstance(sample_scoring_function, str):
                if sample_scoring_function not in ["prefix", "suffix", "full"]:
                    raise ValueError(
                        f"type_exact_match (used in parametrized_exact_match) must be one of prefix, suffix, or full. Was {sample_scoring_function} instead."
                    )
                self.type_exact_match = sample_scoring_function
            else:
                self.type_exact_match = "full"
            self.score_sample = self.default_sample_scoring

    def compute(self, doc: Doc, model_response: ModelResponse, **kwargs) -> float:
        """Computes the metric over a list of golds and predictions for one single item with possibly many samples.
        It applies normalisation (if needed) to model prediction and gold, computes their per prediction score,
        then aggregates the scores over the samples using a pass@k.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): k predicted strings

        Returns:
            float: Aggregated score over the current sample's items.
        """
        golds = doc.get_golds()
        predictions = model_response.text
        if len(golds) > 1:
            raise Exception("Cannot compute pass@k with several golds")

        if self.n is None:
            self.n = len(predictions)
            logger.warning("n undefined in the pass@k. We assume it's the same as the sample's number of predictions.")
        elif len(predictions) < self.n:
            logger.warning(f"Number of predictions is less than {self.n} for pass@k.")

        processed_choices = [self.get_processed_gold(gold=g) for g in doc.choices]
        new_doc = Doc(
            choices=processed_choices,
            query=doc.query,
            gold_index=doc.gold_index,
        )

        all_scores = []
        for pred in predictions[: self.n]:
            cur_pred = self.get_processed_pred(pred=pred)
            new_model_response = ModelResponse(
                text=[cur_pred],
            )
            all_scores.append(self.score_sample(new_doc, new_model_response))

        return self.pass_at_k(all_scores)

    def get_processed_gold(self, gold: str) -> str:
        if self.strip_strings:
            gold = gold.strip()

        if self.normalize_gold:
            gold = self.normalize_gold(gold)

        return gold

    def get_processed_pred(self, pred: str) -> str:
        if not pred:
            return ""

        if self.strip_strings:
            pred = pred.strip()

        if self.normalize_pred:
            pred = self.normalize_pred(pred)

        return pred

    def default_sample_scoring(self, doc, model_response) -> int:
        pred = model_response.text[0]
        gold = doc.get_golds()[0]

        if self.type_exact_match == "prefix":
            return 1 if pred.startswith(gold) else 0
        if self.type_exact_match == "suffix":
            return 1 if pred.endswith(gold) else 0
        return 1 if gold == pred else 0

    def pass_at_k(self, all_scores: list[int]) -> float:
        """Algo from https://arxiv.org/pdf/2107.03374"""
        c: int = all_scores.count(1)
        if self.n - c < self.k:
            return 1.0

        return 1.0 - np.prod(1.0 - self.k / np.arange(self.n - c + 1, self.n + 1))


class GPassAtK:
    def __init__(
        self,
        k: Union[int, list[int]],
        n: int | None = None,
        thresholds: list[float] = [0.0, 0.25, 0.5, 0.75, 1.0],
        normalize_gold: Callable | None = None,
        normalize_pred: Callable | None = None,
        strip_strings: bool = False,
        sample_scoring_function: Callable[[Doc, ModelResponse], float] | str | None = None,
    ):
        """Computing G-Pass@k from http://arxiv.org/abs/2412.13147

        Args:
            k (int, list): The number of successful attempts to be considered.
            n (int): Number of samples to generate.
            thresholds (list): Thresholds to control successful attempts in k generate.
            normalize_gold (callable, optional): Function to use to normalize the reference strings.
                Defaults to None if no normalization is applied.
            normalize_pred (callable, optional): Function to use to normalize the predicted strings.
                Defaults to None if no normalization is applied.
            strip_strings (bool, optional): Whether to strip both reference and predictions. Defaults to False.
            sample_scoring_function (callable or str, optional): Function to use to score each sample.
                Either pass the full function (should take a string prediction and a string gold, and return a score between 0 and 1)
                a string (any of `prefix`, `suffix` or `full`) to define the type of exact match that you want, or nothing to defaults to "full".
                    `prefix` checks if the prediction starts with the gold,
                    `suffix` if the prediction ends with the gold,
                    `full` if the prediction and gold are equal
        """
        self.k = as_list(k)
        self.n = n
        self.thresholds = thresholds
        self.normalize_gold = normalize_gold
        self.normalize_pred = normalize_pred
        self.strip_strings = strip_strings

        # Managed the logic of the per prediction of sample scoring
        if callable(sample_scoring_function):
            self.score_sample = sample_scoring_function
            self.type_exact_match = None
        else:
            if isinstance(sample_scoring_function, str):
                if sample_scoring_function not in ["prefix", "suffix", "full"]:
                    raise ValueError(
                        f"type_exact_match (used in parametrized_exact_match) must be one of prefix, suffix, or full. Was {sample_scoring_function} instead."
                    )
                self.type_exact_match = sample_scoring_function
            else:
                self.type_exact_match = "full"
            self.score_sample = self.default_sample_scoring

    def compute(self, model_response: ModelResponse, doc: Doc, **kwargs) -> float:
        """Computes the metric over a list of golds and predictions for one single item with possibly many samples.
        It applies normalisation (if needed) to model prediction and gold, computes their per prediction score,
        then aggregates the scores over the samples using a pass@k.

        Args:
            golds (list[str]): Reference targets
            predictions (list[str]): k predicted strings

        Returns:
            float: Aggregated score over the current sample's items.
        """
        golds = doc.get_golds()
        predictions = model_response.text

        if len(golds) > 1:
            raise Exception("Cannot compute G-Pass@k with several golds")

        if self.n is None:
            self.n = len(predictions)
            logger.warning(
                "n undefined in the G-Pass@k. We assume it's the same as the sample's number of predictions."
            )
        elif len(predictions) < self.n:
            logger.warning(f"Number of predictions is less than {self.n} for G-Pass@k.")

        processed_choices = [self.get_processed_gold(gold=g) for g in doc.choices]
        new_doc = Doc(
            choices=processed_choices,
            query=doc.query,
            gold_index=doc.gold_index,
        )

        all_scores = []
        for pred in predictions[: self.n]:
            cur_pred = self.get_processed_pred(pred=pred)
            new_model_response = ModelResponse(
                text=[cur_pred],
            )
            all_scores.append(self.score_sample(new_doc, new_model_response))

        return self.g_pass_at_k(all_scores)

    def get_processed_gold(self, gold: str) -> str:
        if self.strip_strings:
            gold = gold.strip()

        if self.normalize_gold:
            gold = self.normalize_gold(gold)

        return gold

    def get_processed_pred(self, pred: str) -> str:
        if not pred:
            return ""

        if self.strip_strings:
            pred = pred.strip()

        if self.normalize_pred:
            pred = self.normalize_pred(pred)

        return pred

    def default_sample_scoring(self, doc: Doc, model_response: ModelResponse) -> int:
        gold = doc.get_golds()[0]
        pred = model_response.text[0]
        if self.type_exact_match == "prefix":
            return 1 if pred.startswith(gold) else 0
        if self.type_exact_match == "suffix":
            return 1 if pred.endswith(gold) else 0
        return 1 if gold == pred else 0

    def g_pass_at_k(self, all_scores: list[int]) -> float:
        """Computation of G-Pass@k details from http://arxiv.org/abs/2412.13147"""
        c: int = sum(all_scores)
        n: int = self.n
        ks: int = self.k
        thresholds: list[float] = self.thresholds

        def _compute_g_pass_at_k(n, c, k, m):
            if m > min(c, k) or k > n or c < 0 or n <= 0 or m < 0:
                return 0.0
            return hypergeom.sf(m - 1, n, c, k)

        def compute_g_pass_at_k(n, c, k, t):
            m = max(int(np.ceil(k * t)), 1)
            return _compute_g_pass_at_k(n, c, k, m)

        def compute_mg_pass_at_k(n, c, k):
            low, high = int(np.ceil(k * 0.5)), k

            mg_pass_at_k = 0.0
            for i in range(low + 1, high + 1):
                mg_pass_at_k += _compute_g_pass_at_k(n, c, k, i)
            mg_pass_at_k = 2 * mg_pass_at_k / k

            return mg_pass_at_k

        metrics = {}
        for k in ks:
            for t in thresholds:
                metrics[f"G-Pass@{k}_{t}"] = compute_g_pass_at_k(n, c, k, t)
            metrics[f"mG-Pass@{k}"] = compute_mg_pass_at_k(n, c, k)

        return metrics

    @property
    def all_metrics(self):
        ks: int = self.k
        thresholds: list[float] = self.thresholds

        metrics = []
        for k in ks:
            for t in thresholds:
                metrics.append(f"G-Pass@{k}_{t}")
            metrics.append(f"mG-Pass@{k}")

        return metrics
