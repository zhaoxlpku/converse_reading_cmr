import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import numpy as np
import logging
import math
import my_utils.squad_eval as squad_eval
from .san_decoder import SANDecoder
from .recurrent import ContextualEmbed
from collections import defaultdict

from torch.optim.lr_scheduler import *
from torch.autograd import Variable
from my_utils.utils import AverageMeter
from .dreader import DNetwork
from .dreader_seq2seq import DNetwork_Seq2seq
from my_utils.tokenizer import *
logger = logging.getLogger(__name__)

from my_utils import eval_bleu, eval_nist


class myNetwork(nn.Module):
    def __init__(self, encoder, decoder, ans_embedding, generator,
                 loss_compute, enc_dec_bridge):
        super(myNetwork, self).__init__()
        self.encoder = encoder
        self.decoder = decoder
        self.ans_embedding = ans_embedding
        self.generator = generator
        self.loss_compute = loss_compute
        self.enc_dec_bridge = enc_dec_bridge


class DocReaderModel(object):
    def __init__(self, opt, embedding=None, state_dict=None):
        self.opt = opt
        self.updates = state_dict['updates'] if state_dict and 'updates' in state_dict else 0
        self.eval_embed_transfer = True
        self.train_loss = AverageMeter()

        # TODO(1105)
        if self.opt['weight_type'] == 'bleu':
            print('Use BLEU for weighing')
            self.sentence_metric = eval_bleu.sentence_bleu
        elif self.opt['weight_type'] == 'nist':
            print('Use NIST for weighing')
            self.sentence_metric = eval_nist.sentence_nist
        else:
            raise ValueError('Unknown weight type {}'.format(self.opt['weight_type']))

        # TODO(1105)
        if self.opt['model_type'] == 'san':
            encoder = DNetwork(opt, embedding)
        elif self.opt['model_type'] in {'seq2seq', 'memnet'}:
            encoder = DNetwork_Seq2seq(opt, embedding)
        else:
            raise ValueError('Unknown model type: {}'.format(self.opt['model_type']))

        # TODO(02282019)
        if self.opt['model_type'] in {'seq2seq', 'memnet'}:
            self.cove_embedder = ContextualEmbed(opt['covec_path'], opt['vocab_size'], embedding=embedding)
        else:
            self.cove_embedder = None

        # TODO(q)(added)
        #decoder_hidden_size = encoder.query_understand.output_size
        decoder_hidden_size = opt['decoder_hidden_size']
        enc_dec_bridge = nn.Linear(
            #encoder.query_understand.output_size,
            encoder.hidden_size,
            decoder_hidden_size)

        if opt['self_attention_on']:
            doc_mem_hidden_size = encoder.doc_mem_gen.output_size
        else:
            doc_mem_hidden_size = encoder.doc_understand.output_size

        decoder = SANDecoder(doc_mem_hidden_size,
                             decoder_hidden_size,
                             opt,
                             prefix='decoder',
                             dropout=encoder.dropout)
        ans_embedding = nn.Embedding(opt['vocab_size'],
                                     doc_mem_hidden_size,
                                     padding_idx=0)

        print('decoder hidden size: %d' % decoder_hidden_size)
        print('ans emb size: %d' % doc_mem_hidden_size)

        generator = nn.Sequential(nn.Linear(decoder_hidden_size, opt['vocab_size']),
                                  nn.LogSoftmax())

        loss_compute = nn.NLLLoss(ignore_index=0)

        self.network = myNetwork(encoder, decoder, ans_embedding, generator,
                                 loss_compute, enc_dec_bridge)
        if state_dict:
            print ('loading checkpoint model...')
            new_state = set(self.network.state_dict().keys())
            for k in list(state_dict['network'].keys()):
                if k not in new_state:
                    del state_dict['network'][k]
            for k, v in list(self.network.state_dict().items()):
                if k not in state_dict['network']:
                    state_dict['network'][k] = v
            self.network.load_state_dict(state_dict['network'])

        # Building optimizer.
        parameters = [p for p in self.network.parameters() if p.requires_grad]

        if opt['optimizer'] == 'sgd':
            self.optimizer = optim.SGD(parameters, opt['learning_rate'],
                                       momentum=opt['momentum'],
                                       weight_decay=opt['weight_decay'])
        elif opt['optimizer'] == 'adamax':
            self.optimizer = optim.Adamax(parameters,
                                          opt['learning_rate'],
                                          weight_decay=opt['weight_decay'])
        elif opt['optimizer'] == 'adam':
            self.optimizer = optim.Adam(parameters,
                                        opt['learning_rate'],
                                        weight_decay=opt['weight_decay'])
        elif opt['optimizer'] == 'adadelta':
            self.optimizer = optim.Adadelta(parameters,
                                            opt['learning_rate'],
                                            rho=0.95)
        else:
            raise RuntimeError('Unsupported optimizer: %s' % opt['optimizer'])
        if state_dict and 'optimizer' in state_dict:
            self.optimizer.load_state_dict(state_dict['optimizer'])

        if opt['fix_embeddings']:
            wvec_size = 0
        else:
            # TODO(1105)
            #wvec_size = (opt['vocab_size'] - opt['tune_partial']) * opt['embedding_dim']
            wvec_size = 0
        if opt.get('have_lr_scheduler', False):
            if opt.get('scheduler_type', 'rop') == 'rop':
                self.scheduler = ReduceLROnPlateau(self.optimizer, mode='min', factor=opt['lr_gamma'], patience=2, verbose=True)
            elif opt.get('scheduler_type', 'rop') == 'exp':
                self.scheduler = ExponentioalLR(self.optimizer, gamma=opt.get('lr_gamma', 0.5))
            else:
                milestones = [int(step) for step in opt.get('multi_step_lr', '10,20,30').split(',')]
                self.scheduler = MultiStepLR(self.optimizer, milestones=milestones, gamma=opt.get('lr_gamma'))
        else:
            self.scheduler = None
        self.total_param = sum([p.nelement() for p in parameters]) - wvec_size


    ## TODO(0809): RNN encoder + memory
    def encode_memnet(self, query, batch):
        if self.opt['cuda']:
            query = query.cuda()
        query_emb = self.network.encoder.embedding(query)
        encoder_hidden = self.network.encoder.initHidden(query.size(1))
        if self.opt['cuda']:
            encoder_hidden = encoder_hidden.cuda()
            query_emb = query_emb.cuda()
        encoder_hidden = Variable(encoder_hidden)
        for word in torch.split(query_emb, 1):
            word = word.squeeze(0)
            encoder_hidden = self.network.encoder(word, encoder_hidden)

        mem_hidden = self.network.encoder.add_fact_memory(encoder_hidden, batch)
        mem_hidden += encoder_hidden

        return mem_hidden

    # TODO(02282019)
    def patch(self, v):
        if self.opt['cuda']:
            v = Variable(v.cuda(async=True))
        else:
            v = Variable(v)
        return v

    ##  RNN encoder
    def encode(self, query, batch):
        if self.opt['cuda']:
            query = query.cuda()
        query_emb = self.network.encoder.embedding(query)

        # TODO(02282019)
        #query_cove_low, query_cove_high = self.cove_embedder(self.patch(batch['query_tok']), self.patch(batch['query_mask']))
        query_cove_low, query_cove_high = self.cove_embedder(Variable(batch['query_tok']), Variable(batch['query_mask']))
        if self.opt['cuda']:
            query_cove_low = query_cove_low.cuda()
            query_cove_high = query_cove_high.cuda()
        query_cove_low = query_cove_low.transpose(1, 0, 2)
        query_cove_high = query_cove_high.transpose(1, 0, 2)
        query_emb = torch.cat([query_emb, query_cove_low, query_cove_high], 2)

        encoder_hidden = self.network.encoder.initHidden(query.size(1))
        if self.opt['cuda']:
            encoder_hidden = encoder_hidden.cuda()
        encoder_hidden = Variable(encoder_hidden)
        for word in torch.split(query_emb, 1):
            word = word.squeeze(0)
            encoder_hidden = self.network.encoder(word, encoder_hidden)
        return encoder_hidden

    def compute_w(self, fact, res, smooth=0, batch_size=32):
        def _strip_pad(lst):
            lst = [str(_) for _ in lst]
            lst = ' '.join(lst)
            lst = lst.strip(' 0')
            lst = lst.split()
            return lst

        w = []
        for f, r in zip(fact, res):
            #fact_bleu = self.sentence_metric(f, r)
            f = _strip_pad(f)
            r = _strip_pad(r)
            # fact_bleu = squad_eval.get_bleu_moses_score([f], [r])
            fact_bleu = self.sentence_metric([f], r, smooth=True)
            # print (fact_bleu)
            fact_bleu += smooth
            w.append(fact_bleu)
        # print("fact {}".format(fact))
        # print("res {}".format(res))
        w = np.array(w)
        w = w / sum(w)
        w = w * batch_size
        return w


    def update(self, batch, smooth=-1, rep_train=0.5):
        self.network.train()
        if rep_train > 0:
            rep_train = 1 - rep_train
            rep_len = int(len(batch['doc_tok'])*rep_train)
            answer_token = batch['answer_token'][:rep_len]
            doc_tok = batch['doc_tok'][rep_len:]
            ans_len = len(batch['answer_token'][1])
            doc_tok = doc_tok[:, :ans_len]
            doc_ans = torch.cat((answer_token, doc_tok), 0)
            doc_ans = Variable(doc_ans.transpose(0, 1),
                               requires_grad=False) # TODO(q)(added)
        else:
            doc_ans = Variable(batch['answer_token'].transpose(0, 1),
                           requires_grad=False) # TODO(q)(added)
        # doc_ans = (torch.LongTensor(torch.from_numpy(np.ones(shape=[2, len(batch['answer_token'])], dtype=np.int64)*5)))
        # doc_ans = Variable(doc_ans)
        # print(doc_ans)
        # ans_mask = Variable(batch['ans_mask'].transpose(0, 1))
        if self.opt['cuda']:
            doc_ans = doc_ans.cuda()
            # ans_mask = ans_mask.cuda()
        doc_ans_emb = self.network.ans_embedding(doc_ans)

        # TODO(q)
        # TODO(0809)
        # TODO(1105)
        if self.opt['model_type'] == 'san':
            doc_mem, query_mem, doc_mask = self.network.encoder(batch)
        elif self.opt['model_type'] in {'seq2seq', 'memnet'}:
            query = Variable(batch['query_tok'].transpose(0, 1))
            if self.opt['model_type'] == 'seq2seq':
                encoder_hidden = self.encode(query, batch)
            else:
                encoder_hidden = self.encode_memnet(query, batch)
            query_mem = encoder_hidden
            doc_mem, doc_mask = None, None
        else:
            raise ValueError('Unknown model type: {}'.format(self.opt['model_type']))

        batch_size = query_mem.size(0)

        # TODO(q)
        #hidden = query_mem
        hidden = self.network.enc_dec_bridge(query_mem)

        hiddens = []

        for word in torch.split(doc_ans_emb, 1)[:-1]:
            word = word.squeeze(0)
            hidden = self.network.decoder(word, hidden, doc_mem, doc_mask)
            hiddens.append(hidden)
        hiddens = torch.stack(hiddens)
        log_probs = self.network.generator(hiddens.view(-1, hiddens.size(2)))

        if smooth >= 0:
            weight = self.compute_w(batch['doc_tok'],
                batch['answer_token'][:, 1:-1], smooth, batch_size)
            #weight = torch.FloatTensor(weight).view(-1,1,1).cuda()
            weight = np.reshape(weight, [-1, 1, 1])
            weight = torch.FloatTensor(weight).cuda()
            weight = Variable(weight, requires_grad=False)
            new_log_probs = log_probs.view(batch_size, -1, self.opt['vocab_size'])
            new_log_probs = weight * new_log_probs
            log_probs = new_log_probs.view(-1, self.opt['vocab_size'])

        # TODO(q)
        target = doc_ans[1:].view(-1).data

        target = Variable(target, requires_grad=False)
        loss = self.network.loss_compute(log_probs, target)
        #loss.data.masked_fill_(1 - ans_mask[1:], 0.)
        #loss = loss.sum() / doc_ans.size(1)
        self.train_loss.update(loss.data[0], doc_ans.size(1))
        ## update loss
        self.optimizer.zero_grad()
        loss.backward()
        torch.nn.utils.clip_grad_norm(self.network.parameters(),
                                      self.opt['grad_clipping'])
        self.optimizer.step()
        self.updates += 1

        # TODO(q)
        #self.reset_embeddings()

        self.eval_embed_transfer = True


    def predict(self, batch, top_k=2):
        max_len = self.opt['max_len']
        BOS_token = STA_ID
        self.network.eval()
        self.network.drop_emb = False

        # TODO(q)
        # TODO(0809)
        # TODO(1105)
        if self.opt['model_type'] == 'san':
            doc_mem, query_mem, doc_mask = self.network.encoder(batch)
        elif self.opt['model_type'] in {'seq2seq', 'memnet'}:
            query = Variable(batch['query_tok'].transpose(0, 1))
            if self.opt['model_type'] == 'seq2seq':
                encoder_hidden = self.encode(query, batch)
            else:
                encoder_hidden = self.encode_memnet(query, batch)
            query_mem = encoder_hidden
            doc_mem, doc_mask = None, None
        else:
            raise ValueError('Unknown model type: {}'.format(self.opt['model_type']))

        # TODO(q)
        #hidden = query_mem
        hidden = self.network.enc_dec_bridge(query_mem)

        batch_size = query_mem.size(0)

        next_token = Variable(torch.LongTensor([BOS_token] * batch_size),
                              requires_grad=False).cuda()


        # TODO(1114)
        def _get_topk_tokens(log_prob, topk):
            """all except `log_prob` must be numpy
            """
            log_prob_py = log_prob.data.cpu().numpy()
            topk_tokens = log_prob_py.argsort()[:, -topk:]
            return topk_tokens

        # TODO(1101)
        fact_py = batch['doc_tok'].numpy().tolist()
        def _delta_bleu(exist_subseq, fact, log_prob, topk):
            """all except `log_prob` must be numpy
            """
            log_prob_py = log_prob.data.cpu().numpy()
            if exist_subseq is None:
                exist_bleu = np.zeros([batch_size])
            else:
                exist_bleu = [self.sentence_metric([r], f, smooth=True)
                              for r, f in zip(exist_subseq, fact)]

            delta_bleu = np.zeros([batch_size, self.opt['vocab_size']])
            # topk_tokens.shape() == [batch_size, topk]
            topk_tokens = log_prob_py.argsort()[:, -topk:]

            if self.opt['decoding_bleu_lambda'] > 0:
                for topk_i in range(topk):
                    candidate_token = topk_tokens[:, topk_i]
                    delta_bleu_i = _delta_bleu_core(candidate_token, exist_subseq, fact, exist_bleu)
                    delta_bleu[range(batch_size), candidate_token] = delta_bleu_i

                    #print("delta_bleu topk {}".format(topk_i))
                    #print(delta_bleu_i)

                if self.opt['decoding_bleu_normalize']:
                    ##TODO(1104) test
                    #print('decoding_bleu_normalize')
                    delta_bleu_sum = np.sum(delta_bleu, axis=1, keepdims=True)
                    delta_bleu /= (delta_bleu_sum + 1e-7)

            return delta_bleu, topk_tokens

        def _delta_bleu_core(candidate_token, exist_subseq, fact, exist_bleu):
            """all inputs must be numpy or python, not pytorch
            """
            candidate_token = np.reshape(candidate_token, [-1, 1])
            if exist_subseq is None:
                new_subseq = candidate_token
            else:
                new_subseq = np.concatenate([exist_subseq, candidate_token], 1)
            new_bleu = [self.sentence_metric([r], f, smooth=True)
                        for r, f in zip(new_subseq, fact)]
            return np.array(new_bleu) - np.array(exist_bleu)
        # ============


        preds = []
        pred_topks = []
        # TOOD(1101)
        preds_np = None
        # ==============
        # TODO(q)
        for step in range(max_len):
        #doc_ans = Variable(batch['answer_token'].transpose(0, 1))
        #if self.opt['cuda']:
        #    doc_ans = doc_ans.cuda()
        #for next_token in torch.split(doc_ans, 1)[:-1]:
        #    next_token = next_token.squeeze(0)
            word = self.network.ans_embedding(next_token)
            hidden = self.network.decoder(word, hidden, doc_mem, doc_mask)
            log_prob = self.network.generator(hidden)
            # TODO(q)
            unk_id = self.opt['unk_id']
            rm_UNK = torch.cat([torch.zeros([batch_size, unk_id]),
                                torch.ones([batch_size, 1])*-1000000,
                                torch.zeros([batch_size, self.opt['vocab_size']-unk_id-1])], dim=1).float()
            log_prob += Variable(rm_UNK, requires_grad=False).cuda()

            # TODO(q)
            if self.opt['decoding'] == 'greedy':
                _, next_token = torch.max(log_prob, 1)
            elif self.opt['decoding'] == 'sample':
                t = self.opt['temperature']
                next_token = torch.multinomial(torch.exp(log_prob / t), 1, replacement=True).squeeze()
            # TODO(1101)
            elif self.opt['decoding'] == 'weight':
                delta_bleu, log_prob_topk_tokens = _delta_bleu(preds_np, fact_py, log_prob, self.opt['decoding_topk'])
                #log_prob_topk_tokens = torch.LongTensor(log_prob_topk_tokens).cuda()
                effective_log_prob_sum = Variable(torch.zeros([batch_size]), requires_grad=False).cuda()

                #dumb_log_prob = torch.ones(log_prob.size()).cuda()*-10000000
                dumb_log_prob = np.ones(log_prob.size())*-10000000
                for topk_i in range(self.opt['decoding_topk']):
                    log_prob_topk_i = log_prob[torch.LongTensor(range(batch_size)).cuda(), torch.LongTensor(log_prob_topk_tokens[:, topk_i]).cuda()]
                    #dumb_log_prob[torch.LongTensor(range(batch_size)).cuda(), log_prob_topk_tokens[:, topk_i]] = log_prob_topk_i
                    dumb_log_prob[range(batch_size), log_prob_topk_tokens[:, topk_i]] = log_prob_topk_i.data.cpu().numpy()

                    effective_log_prob_sum += log_prob_topk_i

                    ##TODO(1104) test
                    #print("log_prob_topk {}".format(topk_i))
                    #print(log_prob_topk_i)

                dumb_log_prob = Variable(torch.FloatTensor(dumb_log_prob), requires_grad=False).cuda()

                delta_bleu_w = effective_log_prob_sum / self.opt['decoding_topk']

                ##TODO(1104) test
                #print("delta_bleu_w")
                #print(effective_log_prob_sum / self.opt['decoding_topk'])

                delta_bleu_w = delta_bleu_w.view(-1, 1)

                bleu_reweight = delta_bleu_w * Variable(torch.FloatTensor(delta_bleu), requires_grad=False).cuda()

                w_log_prob = dumb_log_prob + self.opt['decoding_bleu_lambda'] * bleu_reweight

                t = self.opt['temperature']
                next_token = torch.multinomial(torch.exp(w_log_prob / t), 1, replacement=True).squeeze()
            # ============
            else:
                raise ValueError('Unknown decoding: %s' % self.opt['decoding'])
            preds.append(next_token.data.cpu().numpy())

            #TODO(1101)
            next_token_np = next_token.data.cpu().numpy()
            next_token_np = np.reshape(next_token_np, [-1, 1])
            if preds_np is None:
                preds_np = next_token_np
            else:
                preds_np = np.concatenate([preds_np, next_token_np], 1)
            # =======

            #TODO(q)
            _, topk_list = torch.topk(log_prob, top_k)
            pred_topks.append(topk_list.data.cpu().numpy())
        #TODO(q)(top)
        prediction_topks = [[p[i] for p in pred_topks] for i in range(batch_size)]

        predictions = [[p[i] for p in preds] for i in range(batch_size)]

        ## TODO(1104) test
        #print('Done')
        #exit()

        return (predictions,  prediction_topks)

    # TODO(q)(added)
    def eval_test_loss(self, batch):
        self.network.eval()
        self.network.drop_emb = False

        doc_ans = Variable(batch['answer_token'].transpose(0, 1),
                           requires_grad=False, volatile=True)
        if self.opt['cuda']:
            doc_ans = doc_ans.cuda()

        doc_ans_emb = self.network.ans_embedding(doc_ans)

        # TODO(q)
        if self.opt['model_type'] == 'san':
            doc_mem, query_mem, doc_mask = self.network.encoder(batch)
        elif self.opt['model_type'] in {'seq2seq', 'memnet'}:
            query = Variable(batch['query_tok'].transpose(0, 1))
            if self.opt['model_type'] == 'seq2seq':
                encoder_hidden = self.encode(query, batch)
            else:
                encoder_hidden = self.encode_memnet(query, batch)
            query_mem = encoder_hidden
            doc_mem, doc_mask = None, None
        else:
            raise ValueError('Unknown model type: {}'.format(self.opt['model_type']))

        # TODO(q)
        #hidden = query_mem
        hidden = self.network.enc_dec_bridge(query_mem)

        hiddens = []
        for word in torch.split(doc_ans_emb, 1)[:-1]:
            word = word.squeeze(0)
            hidden = self.network.decoder(word, hidden, doc_mem, doc_mask)
            hiddens.append(hidden)
        hiddens = torch.stack(hiddens)
        log_probs = self.network.generator(hiddens.view(-1, hiddens.size(2)))

        target = doc_ans[1:].view(-1).data

        target = Variable(target, requires_grad=False, volatile=True)
        loss = self.network.loss_compute(log_probs, target)

        return loss


    def setup_eval_embed(self, eval_embed, padding_idx = 0):
        self.network.encoder.lexicon_encoder.eval_embed = nn.Embedding(eval_embed.size(0),
                                               eval_embed.size(1),
                                               padding_idx = padding_idx)
        self.network.encoder.lexicon_encoder.eval_embed.weight.data = eval_embed
        for p in self.network.encoder.lexicon_encoder.eval_embed.parameters():
            p.requires_grad = False
        self.eval_embed_transfer = True

        if self.opt['covec_on']:
            self.network.encoder.lexicon_encoder.ContextualEmbed.setup_eval_embed(eval_embed)

    def update_eval_embed(self):
        if self.opt['tune_partial'] > 0:
            offset = self.opt['tune_partial']
            self.network.encoder.lexicon_encoder.eval_embed.weight.data[0:offset] \
                = self.network.encoder.lexicon_encoder.embedding.weight.data[0:offset]

    def reset_embeddings(self):
        if self.opt['tune_partial'] > 0:
            offset = self.opt['tune_partial']
            if offset < self.network.encoder.lexicon_encoder.embedding.weight.data.size(0):
                #print(offset)
                #print(self.network.encoder.lexicon_encoder.embedding.weight.data.size(0))
                #print(self.network.encoder.lexicon_encoder.fixed_embedding.size())
                self.network.encoder.lexicon_encoder.embedding.weight.data[offset:] \
                    = self.network.encoder.lexicon_encoder.fixed_embedding

    def save(self, filename, epoch):
        # strip cove
        network_state = dict([(k, v) for k, v in self.network.state_dict().items() if k[0:4] != 'CoVe'])
        if 'eval_embed.weight' in network_state:
            del network_state['eval_embed.weight']
        if 'fixed_embedding' in network_state:
            del network_state['fixed_embedding']
        params = {
            'state_dict': {'network': network_state},
            'config': self.opt,
        }
        torch.save(params, filename)
        logger.info('model saved to {}'.format(filename))

    def cuda(self):
        self.network.cuda()

    def position_encoding(self, m, threshold=4):
        encoding = np.ones((m, m), dtype=np.float32)
        for i in range(m):
            for j in range(i, m):
                if j - i > threshold:
                    encoding[i][j] = float(1.0 / math.log(j - i + 1))
        return torch.from_numpy(encoding)