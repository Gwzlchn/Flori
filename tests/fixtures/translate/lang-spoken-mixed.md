好那我们今天来聊一下这个 fine-tuning 的事情,其实很多同学一上来就想 full fine-tune,但是说实话你的 GPU 根本扛不住对吧
所以一般我会建议先试试 LoRA,就是 low-rank adaptation,你只需要训练很少的参数,效果其实也还 OK
然后 learning rate 这块大家要注意,不要直接抄 paper 里的数,你的 batch size 不一样嘛,最好自己 sweep 一下
还有一个坑就是 tokenizer,你换了 base model 一定要记得换 tokenizer,不然 loss 看起来在降,但是生成出来全是乱码
最后我们看一下 eval,我一般会准备一个小的 held-out set,每隔几百个 step 跑一次,看看有没有 overfitting
