# 线性回归的闭式解

线性回归假设输出是输入特征的线性组合,我们希望找到一组参数,使预测值与真实值之间的平方误差之和最小。把所有样本写成矩阵形式以后,目标函数可以写成下面的样子。

$$
L(\mathbf{w}) = \lVert \mathbf{X}\mathbf{w} - \mathbf{y} \rVert_2^2 = (\mathbf{X}\mathbf{w} - \mathbf{y})^\top (\mathbf{X}\mathbf{w} - \mathbf{y})
$$

对参数求导并令导数为零,可以得到正规方程 $\mathbf{X}^\top \mathbf{X} \mathbf{w} = \mathbf{X}^\top \mathbf{y}$,当 $\mathbf{X}^\top \mathbf{X}$ 可逆时,闭式解为 $\mathbf{w}^\ast = (\mathbf{X}^\top \mathbf{X})^{-1} \mathbf{X}^\top \mathbf{y}$。下面用 `numpy` 实现一遍,并和 `sklearn` 的结果对比。

```python
import numpy as np
from sklearn.linear_model import LinearRegression

def closed_form(features, targets):
    gram = features.T @ features
    return np.linalg.solve(gram, features.T @ targets)

rng = np.random.default_rng(seed=0)
features = rng.normal(size=(1000, 8))
weights = rng.normal(size=8)
targets = features @ weights + 0.01 * rng.normal(size=1000)
model = LinearRegression(fit_intercept=False).fit(features, targets)
assert np.allclose(closed_form(features, targets), model.coef_, atol=1e-6)
```

需要注意,实际计算时不要直接求逆矩阵,而应该解线性方程组,这样数值上更稳定。当特征数量很大或者矩阵接近奇异时,还可以加入正则项,也就是岭回归,它的闭式解只是在对角线上多加了一个常数。理解闭式解的推导过程,有助于我们看清梯度下降到底在逼近什么。
