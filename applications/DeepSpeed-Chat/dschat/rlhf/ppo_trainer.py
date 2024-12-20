# Copyright (c) Microsoft Corporation.
# SPDX-License-Identifier: Apache-2.0

# DeepSpeed Team
import torch
import torch.nn.functional as F
import time
import deepspeed
from deepspeed.runtime.zero.partition_parameters import ZeroParamStatus
from deepspeed.accelerator import get_accelerator

from dschat.utils.utils import print_rank_0
from transformers import pipeline


def print_all_ranks(tag, value, rank):
    world_size = torch.distributed.get_world_size()
    all_tensor = torch.zeros(world_size, dtype=torch.float32).to(
        get_accelerator().current_device_name())
    all_tensor[rank] = value
    torch.distributed.all_reduce(all_tensor, op=torch.distributed.ReduceOp.SUM)
    print_rank_0(f'{tag} {all_tensor}', rank)


def get_model_norm(model):
    with torch.no_grad():
        total = 0.0
        for param in model.parameters():
            should_gather = hasattr(
                param,
                'ds_id') and param.ds_status == ZeroParamStatus.NOT_AVAILABLE
            with deepspeed.zero.GatheredParameters(param,
                                                   enabled=should_gather):
                total += float(param.float().norm())

    return total


def gather_log_probs(logits, labels):
    log_probs = F.log_softmax(logits, dim=-1)
    log_probs_labels = log_probs.gather(dim=-1, index=labels.unsqueeze(-1))
    return log_probs_labels.squeeze(-1) # returns probabilities of the chosen words


class DeepSpeedPPOTrainer():

    def __init__(self, rlhf_engine, args):
        self.rlhf_engine = rlhf_engine
        self.actor_model = self.rlhf_engine.actor
        self.fact_critic_model = self.rlhf_engine.critic_fact
        self.gen_critic_model = self.rlhf_engine.critic_gen
        self.ref_model = self.rlhf_engine.ref
        self.fact_reward_model = self.rlhf_engine.reward_fact
        self.gen_reward_model = self.rlhf_engine.reward_gen
        self.tokenizer = self.rlhf_engine.tokenizer
        self.args = args
        self.max_answer_seq_len = args.max_answer_seq_len
        self.end_of_conversation_token_id = self.tokenizer(
            args.end_of_conversation_token)['input_ids'][-1]
        self.z3_enabled = args.actor_zero_stage == 3
        self.compute_fp32_loss = self.args.compute_fp32_loss
        self.fact_gen_classifier = pipeline("zero-shot-classification", model="facebook/bart-large-mnli")
        self.candidate_labels = ['factual question', 'generative question']

        # In case the generated experience is not valid (too short), we use the last valid
        # generated experience. Alternatively, we can skip the step (on all workers).
        # For now, use the last valid experience which is a simpler solution
        self.last_generated_experience = None

        # Those value can be changed
        self.kl_ctl = 0.1
        self.clip_reward_value = 5*2
        self.cliprange = 0.2
        self.cliprange_value = 0.2
        self.gamma = 1.0
        self.lam = 0.95
        self.generate_time = 0.0

    def _generate_sequence(self, prompts, mask, step):

        max_min_length = self.max_answer_seq_len + prompts.shape[1]

        # This has been added due to a probability/nan error that happens after
        # meta-llama/Llama-2-7b-hf enabled do_sample:
        # https://huggingface.co/meta-llama/Llama-2-7b-hf/commit/6fdf2e60f86ff2481f2241aaee459f85b5b0bbb9
        if self.actor_model.module.config.model_type == "llama":
            kwargs = dict(do_sample=False)
        else:
            kwargs = dict()

        with torch.no_grad():
            seq = self.actor_model.module.generate(
                prompts,
                attention_mask=mask,
                max_length=max_min_length,
                pad_token_id=self.tokenizer.pad_token_id,
                synced_gpus=self.z3_enabled,
                **kwargs)

        # Filter out seq with no answers (or very short). This happens when users directly use the pre-training ckpt without supervised finetuning
        # NOTE: this will causes each GPU has different number of examples
        batch_size = seq.shape[0]
        prompt_length = prompts.shape[1]
        self.prompt_length = prompt_length
        ans = seq[:, prompt_length:]
        valid_ans_len = (ans != self.tokenizer.pad_token_id).sum(dim=-1)

        if self.args.print_answers and (step % self.args.print_answers_interval
                                        == 0):
            print(
                f"--- prompt --> step={step}, rank={torch.distributed.get_rank()}, {self.tokenizer.batch_decode(prompts, skip_special_tokens=True)}"
            )
            print(
                f"--- ans    --> step={step}, rank={torch.distributed.get_rank()}, {self.tokenizer.batch_decode(ans, skip_special_tokens=True)}"
            )

        out_seq = []
        for i in range(batch_size):
            if valid_ans_len[
                    i] <= 1:  # if the answer is shorter than 1 token, drop it
                print(
                    f'Dropping too short generated answer: {step=}: \n'
                    f'prompts: {self.tokenizer.batch_decode(prompts, skip_special_tokens=False)}\n'
                    f'answers: {self.tokenizer.batch_decode(ans, skip_special_tokens=False)}'
                )
                continue
            else:
                out_seq.append(seq[i:i + 1])

        if not out_seq:
            print(
                f'All generated results are too short for rank={self.args.local_rank} step={step}\n'
                f'-> prompts: {self.tokenizer.batch_decode(prompts, skip_special_tokens=False)}\n'
                f'-> answers: {self.tokenizer.batch_decode(ans, skip_special_tokens=False)}'
            )
            return None

        out_seq = torch.cat(out_seq, dim=0)  # concat output in the batch dim

        return out_seq

    def generate_experience(self, prompts, mask, step):
        self.eval()
        generate_start = time.time()
        seq = self._generate_sequence(prompts, mask, step) # generated using current actor
        generate_end = time.time()
        if seq is None:
            assert self.last_generated_experience is not None, f'Invalid generated experience at {step=}'
            prompts = self.last_generated_experience['prompts']
            seq = self.last_generated_experience['seq']
        else:
            self.last_generated_experience = {'prompts': prompts, 'seq': seq}
        self.train()

        pad_token_id = self.tokenizer.pad_token_id
        attention_mask = seq.not_equal(pad_token_id).long()
        with torch.no_grad():
            output = self.actor_model(seq, attention_mask=attention_mask)
            output_ref = self.ref_model(seq, attention_mask=attention_mask)
            fact_reward_score = self.fact_reward_model.forward_value(
                seq, attention_mask,
                prompt_length=self.prompt_length)['chosen_end_scores'].detach(
                ) # gives the score for each prompt-answer pair in generated experiences
            gen_reward_score = self.gen_reward_model.forward_value(
                seq, attention_mask,
                prompt_length=self.prompt_length)['chosen_end_scores'].detach(
                )
            fact_values = self.fact_critic_model.forward_value(
                seq, attention_mask, return_value_only=True).detach()[:, :-1]
            gen_values = self.gen_critic_model.forward_value(
                seq, attention_mask, return_value_only=True).detach()[:, :-1]

        logits = output.logits
        logits_ref = output_ref.logits
        if self.compute_fp32_loss:
            logits = logits.to(torch.float)
            logits_ref = logits_ref.to(torch.float)

        self.generate_time = generate_end - generate_start

        return {
            'prompts': prompts, #
            'logprobs': gather_log_probs(logits[:, :-1, :], seq[:, 1:]), # probs of chosen sequence with actor #
            'ref_logprobs': gather_log_probs(logits_ref[:, :-1, :], seq[:,
                                                                        1:]), # # probs of chosen sequence iwth reference supervised trained model
            'fact_value': fact_values, # value at each step of generation #
            'gen_value': gen_values, #
            'fact_rewards': fact_reward_score, # # scores for the whole prompt-answer by fact and gen
            'gen_rewards': gen_reward_score, #
            'input_ids': seq, # prompt-answer generated using current actor #
            "attention_mask": attention_mask #
        }

    def compute_rewards(self, prompts, log_probs, ref_log_probs, reward_score,
                        action_mask):

        kl_divergence_estimate = -self.kl_ctl * (log_probs - ref_log_probs)
        rewards = kl_divergence_estimate
        start = prompts.shape[1] - 1
        ends = start + action_mask[:, start:].sum(1) + 1 # that action mask term sum gives the number of non padding words in the generated answer
        reward_clip = torch.clamp(torch.tensor(reward_score), -self.clip_reward_value,
                                  self.clip_reward_value)
        batch_size = log_probs.shape[0]
        for j in range(batch_size):
            rewards[j, start:ends[j]][-1] += reward_clip[j]

        return rewards

    def train_rlhf(self, inputs):
        # train the rlhf mode here
        ### process the old outputs
        prompts = inputs['prompts']
        log_probs = inputs['logprobs']
        ref_log_probs = inputs['ref_logprobs']
        fact_reward_score = inputs['fact_rewards']
        gen_reward_score = inputs['gen_rewards']
        fact_values = inputs['fact_value']
        gen_values = inputs['gen_value']
        attention_mask = inputs['attention_mask']
        seq = inputs['input_ids']
        start = prompts.size()[-1] - 1
        action_mask = attention_mask[:, 1:]

        fact_old_values = fact_values
        gen_old_values = gen_values
        with torch.no_grad():
            prompt_sentences = self.tokenizer.batch_decode(prompts, skip_special_tokens=True)
            ls_of_dic = self.fact_gen_classifier(prompt_sentences, self.candidate_labels)
            fact_prob = []
            gen_prob = []
            for dic in ls_of_dic:
                lab = dic['labels'][0]
                if lab == 'factual question':
                    fact_score = dic['scores'][0]
                    gen_score = dic['scores'][1]
                else:
                    fact_score = dic['scores'][1]
                    gen_score = dic['scores'][0]
                fact_prob.append(fact_score)
                gen_prob.append(gen_score)
            
            weighted_reward_score = [fact_reward_score[j]*fact_prob[j] + gen_reward_score[j]*gen_prob[j] for j in range(len(fact_prob))]
            old_rewards = self.compute_rewards(prompts, log_probs,
                                               ref_log_probs, weighted_reward_score,
                                               action_mask) 

        #     fact_old_rewards = self.compute_rewards(prompts, log_probs,
        #                                        ref_log_probs, fact_reward_score + gen_reward_score,
        #                                        action_mask)
        #     gen_old_rewards = self.compute_rewards(prompts, log_probs,
        #                                        ref_log_probs, gen_reward_score,
        #                                        action_mask)
            ends = start + action_mask[:, start:].sum(1) + 1
            # we need to zero out the reward and value after the end of the conversation
            # otherwise the advantage/return will be wrong
            for i in range(old_rewards.shape[0]):
                old_rewards[i, ends[i]:] = 0
                fact_old_values[i, ends[i]:] = 0
                gen_old_values[i, ends[i]:] = 0
            fact_advantages, fact_returns = self.get_advantages_and_returns(
                fact_old_values, old_rewards, start)
            gen_advantages, gen_returns = self.get_advantages_and_returns(
                gen_old_values, old_rewards, start)            

        ### process the new outputs
        batch = {'input_ids': seq, "attention_mask": attention_mask}
        actor_prob = self.actor_model(**batch, use_cache=False).logits
        actor_log_prob = gather_log_probs(actor_prob[:, :-1, :], seq[:, 1:])
        actor_loss = self.actor_loss_fn(actor_log_prob[:, start:],
                                        log_probs[:, start:], fact_advantages, gen_advantages,
                                        action_mask[:, start:])
        self.actor_model.backward(actor_loss)

        if not self.args.align_overflow:
            self.actor_model.step()

        fact_value = self.fact_critic_model.forward_value(**batch,
                                                return_value_only=True,
                                                use_cache=False)[:, :-1]
        gen_value = self.gen_critic_model.forward_value(**batch,
                                                return_value_only=True,
                                                use_cache=False)[:, :-1]
        fact_critic_loss = self.critic_loss_fn(fact_value[:, start:], fact_old_values[:,
                                                                       start:],
                                          fact_returns, action_mask[:, start:])
        gen_critic_loss = self.critic_loss_fn(gen_value[:, start:], gen_old_values[:,
                                                                       start:],
                                          gen_returns, action_mask[:, start:])
        critic_loss = fact_critic_loss + gen_critic_loss
        self.fact_critic_model.backward(fact_critic_loss)
        self.gen_critic_model.backward(gen_critic_loss)

        if self.args.align_overflow:
            actor_overflow = self.actor_model.optimizer.check_overflow(
                external=True)
            fact_critic_overflow = self.fact_critic_model.optimizer.check_overflow(
                external=True)
            gen_critic_overflow = self.gen_critic_model.optimizer.check_overflow(
                external=True)

            rank = torch.distributed.get_rank()
            if actor_overflow and not fact_critic_overflow and not gen_critic_overflow:
                self.fact_critic_model.optimizer.skip_step = True
                self.gen_critic_model.optimizer.skip_step = True

                print_rank_0(
                    "OVERFLOW: actor overflow, skipping both actor and critic steps",
                    rank)
            elif not actor_overflow and (fact_critic_overflow or gen_critic_overflow):
                self.actor_model.optimizer.skip_step = True
                print_rank_0(
                    "OVERFLOW: critic overflow, skipping both actor and critic steps",
                    rank)
            elif actor_overflow and (fact_critic_overflow or gen_critic_overflow):
                print_rank_0(
                    "OVERFLOW: actor and critic overflow, skipping both actor and critic steps",
                    rank)
            self.actor_model.step()

        self.fact_critic_model.step()
        self.gen_critic_model.step()

        return actor_loss, fact_critic_loss, gen_critic_loss

    def get_overflow(self):
        # Overflow is not expected when using bf16
        # Therefore, DeepSpeed's BF16_Optimizer does not maintain an overflow indication
        if self.args.dtype == "bf16":
            return False, False

        actor_overflow = self.actor_model.optimizer.overflow
        fact_critic_overflow = self.fact_critic_model.optimizer.overflow
        gen_critic_overflow = self.gen_critic_model.optimizer.overflow

        return actor_overflow, fact_critic_overflow, gen_critic_overflow

    def actor_loss_fn(self, logprobs, old_logprobs, fact_advantages, gen_advantages, mask):
        ## policy gradient loss
        log_ratio = (logprobs - old_logprobs) * mask
        ratio = torch.exp(log_ratio)
        fact_pg_loss1 = -fact_advantages * ratio
        fact_pg_loss2 = -fact_advantages * torch.clamp(ratio, 1.0 - self.cliprange,
                                             1.0 + self.cliprange)
        gen_pg_loss1 = -gen_advantages * ratio
        gen_pg_loss2 = -gen_advantages * torch.clamp(ratio, 1.0 - self.cliprange,
                                             1.0 + self.cliprange)
        fact_pg_loss = torch.sum(torch.max(fact_pg_loss1, fact_pg_loss2) * mask) / mask.sum()
        gen_pg_loss = torch.sum(torch.max(gen_pg_loss1, gen_pg_loss2) * mask) / mask.sum()
        pg_loss = fact_pg_loss + gen_pg_loss
        return pg_loss

    def critic_loss_fn(self, values, old_values, returns, mask):
        ## value loss
        values_clipped = torch.clamp(
            values,
            old_values - self.cliprange_value,
            old_values + self.cliprange_value,
        )
        if self.compute_fp32_loss:
            values = values.float()
            values_clipped = values_clipped.float()
        vf_loss1 = (values - returns)**2
        vf_loss2 = (values_clipped - returns)**2
        vf_loss = 0.5 * torch.sum(
            torch.max(vf_loss1, vf_loss2) * mask) / mask.sum()
        return vf_loss

    def get_advantages_and_returns(self, values, rewards, start):
        # Adopted from https://github.com/CarperAI/trlx/blob/main/trlx/models/modeling_ppo.py#L134
        lastgaelam = 0
        advantages_reversed = []
        length = rewards.size()[-1]
        for t in reversed(range(start, length)):
            nextvalues = values[:, t + 1] if t < length - 1 else 0.0
            delta = rewards[:, t] + self.gamma * nextvalues - values[:, t]
            lastgaelam = delta + self.gamma * self.lam * lastgaelam
            advantages_reversed.append(lastgaelam)
        advantages = torch.stack(advantages_reversed[::-1], dim=1)
        returns = advantages + values[:, start:]
        return advantages.detach(), returns

    def _validate_training_mode(self):
        assert self.actor_model.module.training
        assert self.fact_critic_model.module.training
        assert self.gen_critic_model.module.training

    def _validate_evaluation_mode(self):
        assert not self.actor_model.module.training
        assert not self.fact_critic_model.module.training
        assert not self.gen_critic_model.module.training
        assert not self.ref_model.module.training
        assert not self.fact_reward_model.module.training
        assert not self.gen_reward_model.module.training

    def train(self):
        self.actor_model.train()
        self.fact_critic_model.train()
        self.gen_critic_model.train()

    def eval(self):
        self.actor_model.eval()
        self.fact_critic_model.eval()
        self.gen_critic_model.eval()
        self.fact_reward_model.eval()
        self.gen_reward_model.eval()
        self.ref_model.eval()

    def dump_model_norms(self, tag):
        actor_model_norm = get_model_norm(self.actor_model)
        ref_model_norm = get_model_norm(self.ref_model)
        fact_critic_model_norm = get_model_norm(self.fact_critic_model)
        gen_critic_model_norm = get_model_norm(self.gen_critic_model)

        fact_reward_model_norm = get_model_norm(self.fact_reward_model)
        gen_reward_model_norm = get_model_norm(self.gen_reward_model)

        print_all_ranks(f'{tag} global_actor_model_norm', actor_model_norm,
                        self.args.local_rank)
        print_all_ranks(f'{tag} global_ref_model_norm', ref_model_norm,
                        self.args.local_rank)
        print_all_ranks(f'{tag} global_fact_critic_model_norm', fact_critic_model_norm,
                        self.args.local_rank)
        print_all_ranks(f'{tag} global_gen_critic_model_norm', gen_critic_model_norm,
                        self.args.local_rank)
        print_all_ranks(f'{tag} global_fact_reward_model_norm', fact_reward_model_norm,
                        self.args.local_rank)
        print_all_ranks(f'{tag} global_gen_reward_model_norm', gen_reward_model_norm,
                        self.args.local_rank)


class DeepSpeedPPOTrainerUnsupervised(DeepSpeedPPOTrainer):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def train_unsupervised(self, inputs, unsup_coef):
        # Train the unsupervised model here
        self._validate_training_mode()

        outputs = self.actor_model(**inputs, use_cache=False)
        loss = outputs.loss
        self.actor_model.backward(unsup_coef * loss)
        self.actor_model.step()

        return loss
