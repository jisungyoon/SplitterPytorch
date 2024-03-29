import json
import torch
import networkx as nx
import numpy as np
import pandas as pd
from tqdm import tqdm
from walkers import Node2Vec
from torch.utils.data import DataLoader, Dataset
from ego_splitting import EgoNetSplitter
import logging


logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')

class Splitter(torch.nn.Module):
    """
    An implementation of "Splitter: Learning Node Representations that Capture Multiple Social Contexts" (WWW 2019).
    Paper: http://epasto.org/papers/www2019splitter.pdf
    """
    def __init__(self, dimensions, lambd, base_node_count, node_count, device):
        """
        Splitter set up.
        :param dimensions: Dimension of embedding vectors
        :param lambd: Parameter that determine how much personas spread from original embedding
        :param base_node_count: Number of nodes in the source graph.
        :param node_count: Number of nodes in the persona graph.
        :param device: Device which torch use
        """
        super(Splitter, self).__init__()

        self.dimensions = dimensions
        self.lambd = lambd
        self.base_node_count = base_node_count
        self.node_count = node_count
        self.device = device

    def create_weights(self):
        """
        Creating weights for embedding.
        """
        self.base_node_embedding = torch.nn.Embedding(self.base_node_count, self.dimensions, padding_idx = 0)
        self.node_embedding = torch.nn.Embedding(self.node_count, self.dimensions, padding_idx = 0)

    def initialize_weights(self, base_node_embedding, mapping, str2idx):
        """
        Using the base embedding and the persona mapping for initializing the embedding matrices.
        :param base_node_embedding: Node embedding of the source graph.
        :param mapping: Mapping of personas to nodes.
        :param str2idx: Mapping string of original network to index in original network
        """
        persona_embedding = np.array([base_node_embedding[str2idx[original_node]] for node, original_node in mapping.items()])
        self.node_embedding.weight.data = torch.nn.Parameter(torch.Tensor(persona_embedding)).to(self.device)
        self.base_node_embedding.weight.data = torch.nn.Parameter(torch.Tensor(base_node_embedding), requires_grad=False).to(self.device)

    def calculate_main_loss(self, node_f, feature_f, targets):
        """
        Calculating the main loss which is used to learning based on persona random walkers
        It will be act likes centrifugal force from the base embedding
        :param node_f: Embedding vectors of source nodes
        :param feature_f: Embedding vectors of target nodes to predict
        :param targets: Boolean vector whether negative samples or not
        """
        node_f = torch.nn.functional.normalize(node_f, p=2, dim=1)
        feature_f = torch.nn.functional.normalize(feature_f, p=2, dim=1)
        scores = torch.sum(node_f*feature_f, dim=1)
        scores = torch.sigmoid(scores)
        main_loss = targets*torch.log(scores) + (1-targets)*torch.log(1-scores)
        main_loss = -torch.mean(main_loss)
        
        return main_loss

    def calculate_regularization(self, source_f, original_f):
        """
         Calculating the main loss which is used to learning based on persona random walkers
         It will be act likes centripetal force from the base embedding
         :param source_f: Embedding vectors of source nodes
         :param original_f: Embedding vectors of base embedding of source nodes
         """
        source_f = torch.nn.functional.normalize(source_f, p=2, dim=1)
        original_f = torch.nn.functional.normalize(original_f, p=2, dim=1)
        scores = torch.sum(source_f*original_f,dim=1)
        scores = torch.sigmoid(scores)
        regularization_loss = -torch.mean(torch.log(scores))
        
        return regularization_loss

    def forward(self, node_f, feature_f, targets, source_f, original_f):
        """
        1.main loss part
        :param node_f: Embedding vectors of source nodes
        :param feature_f: Embedding vectors of target nodes to predict
        :param targets: Boolean vector whether negative samples or not

        2.regularization part
        :param source_f: Embedding vectors of source nodes
        :param original_f: Embedding vectors of base embedding of source nodes
        """
        main_loss = self.calculate_main_loss(node_f, feature_f, targets)
        regularization_loss = self.calculate_regularization(source_f, original_f)
        loss = main_loss + self.lambd * regularization_loss
        
        return loss


class SplitterTrainer(object):
    """
    Class for training a Splitter.
    """
    def __init__(self, graph, 
                        directed=False,
                        num_walks=10,
                        walk_length=80,
                        p=1,
                        q=1,
                        dimensions=128,
                        window_size=10,
                        base_iter=1,
                        learning_rate=0.01,
                        lambd = 0.1,
                        negative_samples=5,
						size_of_batch=1000,
                        workers=1):
        """
        :param graph: NetworkX graph object.
        :param directed: Directed network(True) or undirected network(False)
        :param num_walks: Number of random walker per node
        :param walk_length: Length(number of nodes) of random walker
        :param p: the likelihood of immediately revisiting a node in the walk
        :param q: search to differentiate between “inward” and “outward” nodes in the walk
        :param dimensions: Dimension of embedding vectors
        :param window_size: Maximum distance between the current and predicted node in the network
        :param base_iter: Number of iterations (epochs) over the walks
        :param learning_rate: Learning rate of Splitter
        :param negative_samples: Number of negative sample in splitter
        :param workers: Number of CPU cores that will be used in training
        """
        self.graph = graph
        self.directed = directed

        self.num_walks = num_walks
        self.walk_length = walk_length
        self.p = p
        self.q = q
        self.dimensions = dimensions 
        self.window_size = window_size
        self.base_iter = base_iter
        self.workers = workers

        self.learning_rate = learning_rate
        self.lambd = lambd
        self.negative_samples = negative_samples 

        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')  

    def create_negative_sample_pool(self):
        """
        Creating the node pools to sample negative samples based on node degree distribution
        """
        self.downsampled_degrees = {node: int(1+self.egonet_splitter.persona_graph.degree(node)**0.75) for node in self.egonet_splitter.persona_graph.nodes()}
        self.negative_samples_pool = [k for k, v in self.downsampled_degrees.items() for i in range(v)]
                  
    def base_model_fit(self):
        """
        Fitting Node2Vec base model.
        """
        self.base_walker = Node2Vec(self.graph,
                                            directed=self.directed,
                                            num_walks=self.num_walks,
                                            walk_length=self.walk_length,
                                            p=self.p,
                                            q=self.q,
                                            dimensions=self.dimensions,
                                            window_size=self.window_size,
                                            base_iter=self.base_iter,
                                            workers=self.workers)
        logging.info("Doing base random walks.")
        self.base_walker.simulate_walks()
        logging.info("Learning the base model.")
        self.base_node_embedding = self.base_walker.learn_embedding()
        del self.base_walker.walks

    def create_split(self):
        """
        Creats the persona networks and generates persona random walker
        """
        self.egonet_splitter = EgoNetSplitter(self.graph)
        self.persona_walker = Node2Vec(self.egonet_splitter.persona_graph,
                                            directed=self.directed,
                                            num_walks=self.num_walks,
                                            walk_length=self.walk_length,
                                            p=self.p,
                                            q=self.q)
        logging.info("Doing persona random walks.")
        self.persona_walker.simulate_walks()
        self.create_negative_sample_pool()

    def setup_model(self):
        """
        Creating a model and initialize the embeddings
        """
        base_node_count = self.graph.number_of_nodes()
        persona_node_count = self.egonet_splitter.persona_graph.number_of_nodes()
        self.model = Splitter(self.dimensions, self.lambd, base_node_count, persona_node_count, self.device)
        self.model.create_weights()
        self.model.initialize_weights(self.base_node_embedding, self.egonet_splitter.personality_map, self.base_walker.str2idx) 

    def reset_node_sets(self):
        """
        Resetting the node sets.
        """
        self.pure_sources = []
        self.personas = []
        self.sources = []
        self.contexts = []
        self.targets = []       

   
    def create_batch_from_path(self, walk):
        source_nodes = [walk[i] for i in range(self.walk_length-self.window_size) for j in range(1,self.window_size+1)]
        context_nodes = [walk[i + j] for i in range(self.walk_length-self.window_size) for j in range(1,self.window_size+1)]
        source_nodes += [walk[i] for i in range(self.window_size,self.walk_length) for j in range(1,self.window_size+1)]
        context_nodes += [walk[i - j] for i in range(self.window_size,self.walk_length) for j in range(1,self.window_size+1)]
        
        length_of_source_nodes = len(source_nodes)
        self.pure_sources += source_nodes
        self.personas += [self.base_walker.str2idx[self.egonet_splitter.personality_map[source_node.item()]] for source_node in source_nodes]
        self.sources += source_nodes * (self.negative_samples + 1)
        self.contexts += context_nodes + list(np.random.choice(self.negative_samples_pool, self.negative_samples * length_of_source_nodes))
        self.targets +=  [1.0] * length_of_source_nodes + [0.0] * (self.negative_samples * length_of_source_nodes)

    def transfer_batch(self):
        """
        Transfering the batch to GPU.
        """
        self.node_f = self.model.node_embedding(torch.LongTensor(self.sources).to(self.device))
        self.feature_f = self.model.node_embedding(torch.LongTensor(self.contexts).to(self.device))
        self.targets = torch.FloatTensor(self.targets).to(self.device)
        self.source_f = self.model.node_embedding(torch.LongTensor(self.pure_sources).to(self.device))
        self.original_f = self.model.base_node_embedding(torch.LongTensor(self.personas).to(self.device))

    def optimize(self):
        """
        Doing a weight update.
        """
        loss = self.model(self.node_f, self.feature_f, self.targets, self.source_f, self.original_f)
        loss.backward()
        self.optimizer.step()
        self.optimizer.zero_grad()
        self.reset_node_sets()
        return loss.item()  

    def fit(self):
        """
        Fitting a model.
        """
        self.reset_node_sets()
        self.base_model_fit()
        self.create_split()
        self.setup_model()
        self.model.train()
        self.optimizer = torch.optim.Adam(self.model.parameters(), lr=self.learning_rate)
        self.optimizer.zero_grad()
        dataset = MyDataset(np.array(self.persona_walker.walks))
        dataloader = DataLoader(dataset, batch_size=100, pin_memory=False, shuffle=True, num_workers=1)

        data_iterator = tqdm(dataloader,
							leave=True,
							unit='batch',
                            postfix={'lss':'% 6f' % 0.0})
        for i, walks in enumerate(data_iterator):
            for walk in walks:
                self.create_batch_from_path(walk)
            self.transfer_batch()
            self.losses = self.optimize()
            data_iterator.set_postfix(lss='%.6f' % self.losses)
			
			
                

    # save functions...
    def save_base_embedding(self, file_name):
        """
        Saving the base node embedding.
        """
        self.base_walker.save_embedding(file_name)      
        
    def save_persona_embedding(self, file_name):
        """
        Saving the persona node embedding.
        """
        logging.info("Saving the model.")
        nodes = [node for node in self.egonet_splitter.persona_graph.nodes()]
        nodes.sort()
        nodes = torch.LongTensor(nodes).to(self.device)
        self.embedding = self.model.node_embedding(nodes).cpu().detach().numpy()
        return_data = {str(node.item()): embedding for node, embedding in zip(nodes, self.embedding)}
        pd.to_pickle(return_data, file_name)
                
    def save_persona_graph_mapping(self, file_name):
        """
        Saving the persona map which is connect original node and personas of node.
        """
        with open(file_name, "w") as f:
            json.dump(self.egonet_splitter.personality_map, f)

    def save_persona_graph(self, file_name):
        """
        Saving the persona graph.
        """
        nx.write_edgelist(self.egonet_splitter.persona_graph, file_name)

        
class MyDataset(Dataset):
    def __init__(self, data):
        self.data = data
        
    def __getitem__(self, index):
        x = self.data[index]
        return x
    
    def __len__(self):
        return len(self.data)
