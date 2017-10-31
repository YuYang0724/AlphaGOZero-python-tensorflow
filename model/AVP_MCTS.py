import copy
import math
import random
import sys
import time

import gtp
import numpy as np

import utils.go as go
import utils.utilities as utils
from utils.features import extract_features,bulk_extract_features

import multiprocessing as mp
import numpy.random.gamma as gamma

# All terminology here (Q, U, N, p_UCT) uses the same notation as in the
# AlphaGo paper.
# Exploration constant
c_PUCT = 5
POOL = mp.Pool()

def split(a, n):
    '''
    Example:
    >>> list(split(range(11), 3))
    [[0, 1, 2, 3], [4, 5, 6, 7], [8, 9, 10]]
    '''
    k, m = divmod(len(a), n)
    return (a[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n))

class AVPMCTSNode():
    '''
    A MCTSNode has two states: plain, and expanded.
    An plain MCTSNode merely knows its Q + U values, so that a decision
    can be made about which MCTS node to expand during the selection phase.
    When expanded, a MCTSNode also knows the actual position at that node,
    as well as followup moves/probabilities via the policy network.
    Each of these followup moves is instantiated as a plain MCTSNode.
    '''
    @staticmethod
    def root_node(position, move_probabilities):
        node = AVPMCTSNode(None, None, 0)
        node.position = position
        node.expand(move_probabilities)
        return node

    def __init__(self, parent, move, prior):
        self.parent = parent # pointer to another MCTSNode
        self.move = move # the move that led to this node
        self.prior = prior
        self.position = None # lazily computed upon expansion
        self.children = {} # map of moves to resulting MCTSNode
        self.Q = self.parent.Q if self.parent is not None else 0 # average of all outcomes involving this node
        self.U = prior # monte carlo exploration bonus
        self.N = 0 # number of times node was visited

    def __repr__(self):
        return "<AVPMCTSNode move=%s prior=%s score=%s is_expanded=%s>" % (self.move, self.prior, self.action_score, self.is_expanded())

    @property
    def action_score(self):
        return self.Q + self.U

    @property
    def action_score_dirichlet(self):
        alpha,ep=0.03,0.25
        return self.Q + self.U / self.prior * ((1-ep)*self.prior+ep*gamma(alpha))

    def is_expanded(self):
        return self.position is not None

    def compute_position(self):
        self.position = self.parent.position.play_move(self.move)
        return self.position

    def expand(self, move_probabilities):
        self.children = {move: MCTSNode(self, move, prob)
            for move, prob in np.ndenumerate(move_probabilities)}
        # Pass should always be an option! Say, for example, seki.
        self.children[None] = MCTSNode(self, None, 0)

    def backup_value(self, value):
        '''
        Since Python lacks Tail Call Optimization(TCO)
        use while loop to reduce the burden of a huge stack
        '''
        while True:
            self.N += 1
            if self.parent is None:
                return
            self.Q, self.U = (
                self.Q + (value - self.Q) / self.N,
                c_PUCT * math.sqrt(self.parent.N) * self.prior / self.N,
            ) # notice sum(all children's N) == parent's N
            # must invert, because alternate layers have opposite desires
            value *= -1
            self = self.parent


    def select_leaf(self):
        current = self
        while current.is_expanded():
            current = max(current.children.values(), key=lambda node: node.action_score)
        return current

    def select_leaf_dirichlet(self):
        current = self
        while current.is_expanded():
            current = max(current.children.values(), key=lambda node: node.action_score_dirichlet)
        return current


class AVPMCTSPlayerMixin:
    def __init__(self, policy_network, seconds_per_move=5):
        self.policy_network = policy_network
        self.seconds_per_move = seconds_per_move
        self.max_rollout_depth = go.N * go.N * 3
        super().__init__()

    def suggest_move(self, position):
        start = time.time()
        move_probs = self.policy_network.run(extract_features(position))
        root = MCTSNode.root_node(position, move_probs)
        while time.time() - start < self.seconds_per_move:
            self.tree_search(root)
        print("Searched for %s seconds" % (time.time() - start), file=sys.stderr)
        sorted_moves = sorted(root.children.keys(), key=lambda move, root=root: root.children[move].N, reverse=True)
        for move in sorted_moves:
            if is_move_reasonable(position, move):
                return move
        return None

    def multi_tree_search(self, root, iters=1600):
        print("tree search", file=sys.stderr)

        # selection
        chosen_leaves = [None]*iters
        for i in range(iters):
            select = lambda root.select_leaf_dirichlet()
            chosen_leaves[i] = POOL.apply_async(select,args=(,))
        positions = [None]*iters
        for i in range(iters):
            chosen_leaf = chosen_leaves[i].get()
            positions[i] = chosen_leaf.compute_position()
            if positions[i] is None:
                print("illegal move!", file=sys.stderr)
                # See go.Position.play_move for notes on detecting legality
                del chosen_leaf.parent.children[chosen_leaf.move]
                continue
            print("Investigating following position:\n%s" % (chosen_leaf.position,), file=sys.stderr)

        # evaluation
        for batch in list(split(range(iters),8)):
            batch_leaves = [chosen_leaves[i] for i in batch]
            leaf_positions = [batch_leaves[i].position for i in range(len(batch_leaves))]
            move_probs,values = self.policy_network.evaluate_node(bulk_extract_features(leaf_positions))
            perspective = []
            for leaf_position in leaf_positions:
                perspective = 1 if leaf_position.to_play == root.position.to_play else -1
                perspectives.append(perspective)
            values = values*np.asarray(perspectives))

            # expansion & backup
            for i in range(len(batch_leaves)):
                batch_leaves[i].expand(move_probs[i])
                print("value: %s" % values[i], file=sys.stderr)
                batch_leaves[i].backup_value(values[i])
        sys.stderr.flush()
        

