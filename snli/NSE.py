import math
import sys
import time
import copy
import numpy as np
import six
from chainer import cuda, Variable, FunctionSet, optimizers
import chainer.functions  as F
import chainer.links as L
import chainer

class NSE(chainer.Chain):

	"""docstring for NSE"""
	def __init__(self, n_units, gpu):
		super(NSE, self).__init__(
			read_lstm = L.LSTM(n_units, n_units),
			write_lstm = L.LSTM(2 * n_units, n_units),
			compose_l1 = F.Linear(2 * n_units, 2 * n_units),
			h_l1 = F.Linear(4 * n_units, 1024),
			l_y = F.Linear(1024, 3))
		self.__n_units = n_units
		self.__gpu = gpu
		self.__mod = cuda.cupy if gpu >= 0 else np
		for param in self.params():
			data = param.data
			data[:] = np.random.uniform(-0.1, 0.1, data.shape)
		if gpu >= 0:
			cuda.get_device(gpu).use()
			self.to_gpu()

	def init_optimizer(self):
		self.__opt = optimizers.Adam(alpha=0.0003, beta1=0.9, beta2=0.999, eps=1e-08)
		self.__opt.setup(self)
		self.__opt.add_hook(chainer.optimizer.GradientClipping(15))
		self.__opt.add_hook(chainer.optimizer.WeightDecay(0.00003))

	def save(self, filename):
		chainer.serializers.save_npz(filename, self)

	@staticmethod
	def load(filename, n_units, gpu):
		self = NSE(n_units, gpu)
		chainer.serializers.load_npz(filename, self)
		return self

	def reset_state(self):
		self.read_lstm.reset_state()
		self.write_lstm.reset_state()


	def read(self, M_t, x_t, batch_size, train):
		"""
		The NSE read operation: Eq. 1-3 in the paper
		"""

		o_t = self.read_lstm(F.dropout(x_t, ratio=0.3, train=train))
		z_t = F.softmax(F.reshape(F.batch_matmul(M_t, o_t), (batch_size, -1)))
		m_t = F.reshape(F.batch_matmul(z_t, M_t, transa=True), (batch_size, -1))
		return o_t, m_t, z_t

	def compose(self, o_t, m_t, train):
		"""
		The NSE compose operation: Eq. 4
		This could be any DNN. Also we could rather compose x_t and m_t. But that is a detail.
		"""

		c_t = self.compose_l1(F.concat([o_t, m_t], axis=1))
		return c_t

	def write(self, M_t, c_t, z_t, full_shape, train):
		"""
		The NSE write operation: Eq. 5 and 6. Here we can write back c_t instead. You could try :)
		"""

		h_t = self.write_lstm(F.dropout(c_t, ratio=0.3, train=train))
		M_t = F.broadcast_to(F.reshape((1 - z_t), (full_shape[0], full_shape[1], 1)), full_shape) * M_t
		M_t += F.broadcast_to(F.reshape(z_t, (full_shape[0], full_shape[1], 1)), full_shape)*F.broadcast_to(F.reshape(h_t, (full_shape[0], 1, full_shape[2])), full_shape)
		return M_t, h_t


	def __forward(self, train, x_batch, y_batch = None):
		n_units = self.__n_units
		mod = self.__mod
		gpu = self.__gpu
		batch_size = len(x_batch)
		x_len = len(x_batch[0])
		
		# lets move it to gpu (could be imporved further to speed up!)
		if gpu >=0:
			x_batch = [[mod.array(e) for e in row] for row in x_batch]
		
		# lets initialize lstm states
		self.reset_state()

		# lets initialize the memory with embeddings
		# Note: many ways to get the memory with shape - (batch_size, x_len, n_units)
		# this could just be the most complex one (can be simplified)
		x_data = mod.concatenate([mod.transpose(mod.concatenate(x_batch[b], axis=0)).reshape((1,n_units,1,x_len)) for b in range(batch_size)], axis=0)
		x = Variable(x_data, volatile=not train)
		x = F.reshape(x, (batch_size,n_units,x_len))
		M_t = F.swapaxes(x, 1, 2)
		
		full_shape = (batch_size, x_len, n_units)
		# attend over future and 
		# schedule composition
		for l in range(x_len):
			# we are just batching the examples
			x_t = []
			for b in range(batch_size):
				x = x_batch[b][l]
				x_t.append(x)
			x_t = Variable(mod.concatenate(x_t, axis=0), volatile=not train)
			#lets read: checkout eq. 1 - 3 in the paper (OR there is a slide at www.tsendeemts.com)
			o_t, m_t, z_t = self.read(M_t, x_t, batch_size, train)
			# and then compose eq. 4
			c_t = self.compose(o_t, m_t, train)
			# finally write eq. 5 and 6
			M_t, h_t = self.write(M_t, c_t, z_t, full_shape, train)
		
		# lets extract the standard features for classification and calculate the loss
		n_hs = F.split_axis(h_t, 2, axis=0)
		hs = F.concat([F.concat(n_hs, axis=1), n_hs[0]-n_hs[1], n_hs[0]*n_hs[1]], axis=1)
		hs = F.relu(self.h_l1(hs))
		y = self.l_y(F.dropout(hs, ratio=0.3, train=train))
		preds = mod.argmax(y.data, 1).tolist()

		accum_loss = 0 if train else None
		if train:
			if gpu >= 0:
				y_batch = cuda.to_gpu(y_batch)
			lbl = Variable(y_batch, volatile=not train)
			accum_loss = F.softmax_cross_entropy(y, lbl)
		
		return preds, accum_loss

	def train(self, x_batch, y_batch):
		self.__opt.zero_grads()
		preds, accum_loss = self.__forward(True, x_batch, y_batch=y_batch)
		# grads are calculated thanks to Chainer!
		accum_loss.backward()
		# params are updated with the optimizer
		self.__opt.update()
		return preds, accum_loss

	def predict(self, x_batch):
		return self.__forward(False, x_batch)[0]