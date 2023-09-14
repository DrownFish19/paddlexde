import paddle
import paddle.nn as nn

from paddlexde.functional import ddeint
from paddlexde.solver.fixed_solver import Euler

B, T, D = 10, 128, 128
series = paddle.randn([B, T, D])  # [10, 128, 128]
t0 = paddle.linspace(0.0, 100.0, T)  # [128]

y0 = series[:, 100:101, :]  # [10, 1, 128]
t_span = t0[100:].expand([B, 28])  # [10, 28]

lags = paddle.randint(low=0, high=100, shape=[B, 20])  # [10, 20]
his = series[:, :100, :]  # [10, 100, 128]
his_span = t0[:100].expand([B, 100])  # [10, 100]

y_tgts = series[:, 100:, :]  # [10, 28, 128]
t_tgts = t0[100:].expand([B, 28])  # [10, 28]


class DDEFunc(nn.Layer):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(D, D * 2)
        self.linear2 = nn.Linear(D * 2, D)

    def forward(self, t, y0, lags, y_lags):
        """_summary_

        Args:
            t (_type_): [B, 1]
            y0 (_type_): [B, 1, D]
            lags (_type_): [B, T] T为不一定连续的时刻序列, 此处样例中为20
            y_lags (_type_): [B, T, D] T为不一定连续的时刻序列, 此处序列与lags相对应

        Returns:
            _type_: _description_
        """
        h = self.linear1(y0)  # [10, 1, 256]
        h_his = self.linear1(y_lags)  # [10, 20, 256]
        h_his = paddle.sum(h_his, axis=1, keepdim=True)  # [10, 1, 256]
        re = self.linear2(h + h_his)  # [10, 1, 128]
        return re


model = DDEFunc()
optimizer = paddle.optimizer.Adam(0.01, parameters=model.parameters())

sol = ddeint(
    func=model,
    y0=y0,
    t_span=t_span,
    lags=lags,
    his=his,
    his_span=his_span,
    solver=Euler,
)

print(sol.shape)
loss = nn.functional.l1_loss(sol, y_tgts)

loss.backward()

optimizer.step()
optimizer.clear_grad()
