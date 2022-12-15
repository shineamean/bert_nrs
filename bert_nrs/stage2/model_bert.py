import numpy as np
import torch
from torch import nn
from utils import MODEL_CLASSES


class AttentionPooling(nn.Module):

    def __init__(self, emb_size, hidden_size):
        super(AttentionPooling, self).__init__()
        self.att_fc1 = nn.Linear(emb_size, hidden_size)
        self.att_fc2 = nn.Linear(hidden_size, 1)

    def forward(self, x, attn_mask=None):
        """
        Args:
            x: batch_size, candidate_size, emb_dim
            attn_mask: batch_size, candidate_size
        Returns:
            (shape) batch_size, emb_dim
        """
        bz = x.shape[0]
        e = self.att_fc1(x)
        e = nn.Tanh()(e)
        alpha = self.att_fc2(e)
        alpha = torch.exp(alpha)

        if attn_mask is not None:
            alpha = alpha * attn_mask.unsqueeze(2)

        alpha = alpha / (torch.sum(alpha, dim=1, keepdim=True) + 1e-8)
        x = torch.bmm(x.permute(0, 2, 1), alpha).squeeze(dim=-1)
        return x


class ScaledDotProductAttention(nn.Module):

    def __init__(self, d_k):
        super(ScaledDotProductAttention, self).__init__()
        self.d_k = d_k

    def forward(self, Q, K, V, attn_mask=None):
        '''
            Q: batch_size, n_head, candidate_num, d_k
            K: batch_size, n_head, candidate_num, d_k
            V: batch_size, n_head, candidate_num, d_v
            attn_mask: batch_size, n_head, candidate_num
            Return: batch_size, n_head, candidate_num, d_v
        '''
        scores = torch.matmul(Q, K.transpose(-1, -2)) / np.sqrt(self.d_k)
        scores = torch.exp(scores)

        if attn_mask is not None:
            scores = scores * attn_mask.unsqueeze(dim=-2)

        attn = scores / (torch.sum(scores, dim=-1, keepdim=True) + 1e-8)
        context = torch.matmul(attn, V)
        return context


class MultiHeadSelfAttention(nn.Module):

    def __init__(self, d_model, n_heads, d_k, d_v):
        super(MultiHeadSelfAttention, self).__init__()
        self.d_model = d_model
        self.n_heads = n_heads
        self.d_k = d_k
        self.d_v = d_v

        self.W_Q = nn.Linear(d_model, d_k * n_heads)
        self.W_K = nn.Linear(d_model, d_k * n_heads)
        self.W_V = nn.Linear(d_model, d_v * n_heads)

        self.scaled_dot_product_attn = ScaledDotProductAttention(self.d_k)
        self._initialize_weights()

    def _initialize_weights(self):
        for m in self.modules():
            if isinstance(m, nn.Linear):
                nn.init.xavier_uniform_(m.weight, gain=1)

    def forward(self, Q, K, V, mask=None):
        '''
            Q: batch_size, candidate_num, d_model
            K: batch_size, candidate_num, d_model
            V: batch_size, candidate_num, d_model
            mask: batch_size, candidate_num
        '''
        batch_size = Q.shape[0]
        if mask is not None:
            mask = mask.unsqueeze(dim=1).expand(-1, self.n_heads, -1)

        q_s = self.W_Q(Q).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        k_s = self.W_K(K).view(batch_size, -1, self.n_heads, self.d_k).transpose(1, 2)
        v_s = self.W_V(V).view(batch_size, -1, self.n_heads, self.d_v).transpose(1, 2)

        context = self.scaled_dot_product_attn(q_s, k_s, v_s, mask)
        output = context.transpose(1, 2).contiguous().view(batch_size, -1, self.n_heads * self.d_v)
        return output


class NewsEncoder(nn.Module):

    def __init__(self, args):
        super(NewsEncoder, self).__init__()
        self.pooling = args.pooling
        config_class, model_class, tokenizer_class = MODEL_CLASSES[args.model_type]
        self.output_index = 3 if args.model_type == 'tnlrv3' else 2
        self.bert_config = config_class.from_pretrained(args.config_name,
                                                        output_hidden_states=True,
                                                        num_hidden_layers=args.num_hidden_layers)
        self.bert_model = model_class.from_pretrained(args.model_name, config=self.bert_config)
        if args.pooling == 'att':
            self.attn = AttentionPooling(self.bert_config.hidden_size, args.news_query_vector_dim)
        self.dense = nn.Linear(self.bert_config.hidden_size, args.news_dim)

    def forward(self, x):
        '''
            x: batch_size, word_num * 2
            mask: batch_size, word_num
        '''
        batch_size, num_words = x.shape
        num_words = num_words // 2
        text_ids = torch.narrow(x, 1, 0, num_words)
        text_attmask = torch.narrow(x, 1, num_words, num_words)
        word_vecs = self.bert_model(
            text_ids, text_attmask)[self.output_index][self.bert_config.num_hidden_layers]
        if self.pooling == 'cls':
            news_vec = torch.narrow(word_vecs, 1, 0, 1).squeeze(dim=1)
        elif self.pooling == 'att':
            news_vec = self.attn(word_vecs)
        else:
            news_vec = torch.mean(word_vecs, dim=1)
        news_vec = self.dense(news_vec)
        return news_vec


class UserEncoder(nn.Module):

    def __init__(self, args):
        super(UserEncoder, self).__init__()
        self.args = args
        if args.model == 'NRMS':
            self.multi_head_self_attn = MultiHeadSelfAttention(args.news_dim,
                                                               args.num_attention_heads, 16, 16)
            self.attn = AttentionPooling(args.num_attention_heads * 16, args.user_query_vector_dim)
        else:
            self.attn = AttentionPooling(args.news_dim, args.user_query_vector_dim)
        self.pad_doc = nn.Parameter(torch.empty(1,
                                                args.news_dim).uniform_(-1,
                                                                        1)).type(torch.FloatTensor)

    def forward(self, news_vecs, log_mask=None):
        '''
            news_vecs: batch_size, history_num, news_dim
            log_mask: batch_size, history_num
        '''
        bz = news_vecs.shape[0]
        if self.args.user_log_mask:
            if self.args.model == 'NRMS':
                news_vecs = self.multi_head_self_attn(news_vecs, news_vecs, news_vecs, log_mask)
                user_vec = self.attn(news_vecs, log_mask)
            else:
                user_vec = self.attn(news_vecs, log_mask)
        else:
            padding_doc = self.pad_doc.unsqueeze(dim=0).expand(bz, self.args.user_log_length, -1)
            news_vecs = news_vecs * \
                log_mask.unsqueeze(dim=-1) + padding_doc * \
                (1 - log_mask.unsqueeze(dim=-1))
            if self.args.model == 'NRMS':
                news_vecs = self.multi_head_self_attn(news_vecs, news_vecs, news_vecs)
                user_vec = self.attn(news_vecs)
            else:
                user_vec = self.attn(news_vecs)
        return user_vec


class ModelBert(torch.nn.Module):

    def __init__(self, args):
        super(ModelBert, self).__init__()
        self.args = args
        self.news_encoder = NewsEncoder(args)
        self.user_encoder = UserEncoder(args)
        self.loss_fn = nn.CrossEntropyLoss()

    def forward(self, history, history_mask, candidate, label):
        '''
            history: batch_size, history_length, num_word_title * 2
            history_mask: batch_size, history_length
            candidate: batch_size, 1+K, num_word_title * 2
            label: batch_size, 1+K
        '''
        batch_size = history.shape[0]
        input_id_num = history.shape[-1]
        candidate_news = candidate.reshape(-1, input_id_num)
        candidate_news_vecs = self.news_encoder(candidate_news).reshape(
            batch_size, -1, self.args.news_dim)

        history_news = history.reshape(-1, input_id_num)
        history_news_vecs = self.news_encoder(history_news).reshape(-1, self.args.user_log_length,
                                                                    self.args.news_dim)

        user_vec = self.user_encoder(history_news_vecs, history_mask)
        score = torch.bmm(candidate_news_vecs, user_vec.unsqueeze(dim=-1)).squeeze(dim=-1)
        loss = self.loss_fn(score, label)
        return loss, score
