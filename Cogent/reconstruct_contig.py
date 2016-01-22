__author__ = 'etseng@pacb.com'

import os
import sys
import time
import logging
from Bio import SeqIO
import networkx as nx

from Cogent import sanity_checks, splice_cycle
from Cogent import splice_graph as sp
from Cogent.Utils import trim_ends, run_external_call, run_gmap, post_gmap_processing, run_gmap_for_final_GFFs
from Cogent.process_path import solve_with_lp_and_reduce, find_minimal_path_needed_to_explain_pathd



sys.setrecursionlimit(999999)


def split_files(input_filename='in.fa', split_size=20):
    """
    Split input files into split_0/in.fa, split_1/in.fa...
    Return the list of split directories
    """
    i = 0
    count = 0
    d = "split_0"
    if not os.path.exists(d):
        os.makedirs(d)
    f = open(os.path.join(d, 'in.fa'), 'w')
    split_dirs = [d]
    run_external_call("cp in.weights {0}/in.weights".format(d))

    for r in SeqIO.parse(open(input_filename), 'fasta'):
        if count >= split_size:
            f.close()
            count = 0
            i += 1
            d = "split_" + str(i)
            if not os.path.exists(d):
                os.makedirs(d)
            split_dirs.append(d)
            run_external_call("cp in.weights {0}/in.weights".format(d))
            f = open(os.path.join(d, 'in.fa'), 'w')
        f.write(">{0}\n{1}\n".format(r.id, r.seq))
        count += 1
    f.close()
    return split_dirs


def run_Cogent_on_split_files(split_dirs):
    """
    1. run Cogent individually on each split directory
    2. combine all aloha2.fa from split directories, run LP against input

    """
    time1 = time.time()
    olddir = os.getcwd()
    for d in split_dirs:
        os.chdir(d)
        run_Cogent_on_input()
        os.chdir(olddir)

    if os.path.exists('combined'):
        run_external_call("rm -rf combined")
    os.makedirs('combined')
    # now combine all the aloha2 results and run LP again
    f = open('combined/aloha.fa', 'w')
    i = 0
    for d in split_dirs:
        for r in SeqIO.parse(open(os.path.join(d, 'aloha2.fa')), 'fasta'):
            f.write(">path{0}\n{1}\n".format(i, r.seq))
            i += 1
    f.close()

    f = open('in.trimmed.fa', 'w')
    for r in SeqIO.parse(open('in.fa'),'fasta'):
        f.write(">{0}\n{1}\n".format(r.id, trim_ends(str(r.seq))))
    f.close()

    os.chdir('combined')
    run_external_call("ln -s ../in.weights in.weights")
    run_external_call("ln -s ../in.trimmed.fa in.trimmed.fa")
    run_gmap()
    post_gmap_processing()
    os.chdir('../')

    # now the result we want is in combined/aloha2.fa, do postprocessing on it with the full in.fa

    run_external_call("ln -f -s combined/aloha2.fa aloha2.fa")
    run_gmap(dbname='aloha2', infile='in.trimmed.fa')
    #post_gmap_processing()

    time4 = time.time()
    log.info("[RUNTIME] Total time in run_Cogent: {0}".format(time4-time1))


def run_Cogent_on_input():
    """
    The main reconstruction function.

    Homopolymers and repeated nodes in path must be resolved first.
    (however, it's possible the graph contains cycles not manifested in path,
     this is a bug that will result in failure to *explain* the sequences later,
     right now I catch the bug by using the sequence pth itself but this should be fixed eventually)

    Graph reduction is iteratively done until cannot be further reduced

    Two points of failure:
    (1) graph is not reduced to small enough, too many paths, mem explosion
        cur soln: fall back to using own paths
    (2) cycle in graph
        cur soln: fall back to using own paths (still wrong)
    """
    time1 = time.time()
    # first trim in.fa away all lower case
    f = open('in.trimmed.fa', 'w')
    for r in SeqIO.parse(open('in.fa'),'fasta'):
        f.write(">{0}\n{1}\n".format(r.id, trim_ends(str(r.seq))))
    f.close()

    seqweights = {}
    # read in the weights for each sequence
    with open('in.weights') as f:
        for line in f:
            seqid, weight = line.strip().split('\t')
            seqweights[seqid] = int(weight)

    # setting up the DiGraph
    G = nx.DiGraph()
    node_d = {None: -1}  # this is just used to initialize the graph, delete it later
    path_d = {}
    reader = SeqIO.parse(open('in.trimmed.fa'),'fasta')
    for r in reader: sp.add_seq_to_graph(G, node_d, path_d, str(r.seq), r.id, seqweights[r.id])
    del node_d[None]
    mermap = dict((v,k) for k,v in node_d.iteritems())

    # resolve all homopolymers
    homo_nodes = filter(lambda n: G.has_edge(n, n), G.nodes_iter())
    for n in homo_nodes:
        sp.untangle_homopolymer_helper(G, path_d, mermap, n)

    splice_cycle.detect_and_replace_cycle(G, path_d, seqweights, mermap, max(G.nodes()), sp.KMER_SIZE)

    visited = {}
    sp.reachability(G, mermap, visited, path_d)

    # cycle detection and abort if detected
    # (this should not happen with splice_cycle.detect_and_replace_cycle run)
    for k,v in path_d.iteritems():
        for x in v:
            if v.count(x) > 1:
                log.info("CYCLE detected! Abort!")
                os.system("touch CYCLE_DETECTED")
                sys.exit(-1)
    #iter = nx.simple_cycles(G)
    #for it in iter:
    #    print >> sys.stderr, "CYCLE detected! Abort!"
    #    os.system("touch CYCLE_DETECTED")
    #    sys.exit(-1)

    nx.write_graphml(G, 'in.0.graphml')

    log.info("Initial Graph Size: {0} nodes, {1} edges".format(G.number_of_nodes(), G.number_of_edges()))

    ## sanity check: confirm that all sequences can be reconstructed via the collapsed graph
    ## also check that all nodes are visited
    #for n in G.nodes_iter(): assert n in visited
    #for k,v in path_d.iteritems():
    #    s = sp.stitch_string_from_path(v, mermap)
    #    s2 = seqdict[k].seq.tostring().upper()
    #    assert s.find(s2) >= 0

    while True:
        cur_num_nodes = G.number_of_nodes()
        sp.find_source_bubbles(G, path_d, mermap)
        sp.reachability(G, mermap, {}, path_d)
        sp.find_bubbles(G, path_d, mermap)
        sp.reachability(G, mermap, {}, path_d)
        sp.contract_sinks(G, path_d, mermap)
        sp.find_dangling_sinks(G, path_d, mermap)
        sp.reachability(G, mermap, {}, path_d)
        if G.number_of_nodes() == cur_num_nodes:
            break

    nx.write_graphml(G, 'in.1.graphml')

    log.info("Post-Reduction Graph Size: {0} nodes, {1} edges".format(G.number_of_nodes(), G.number_of_edges()))

    time2 = time.time()

    keys = path_d.keys()
    keys.sort()
    good_for, paths = find_minimal_path_needed_to_explain_pathd(G, path_d, keys)
    solve_with_lp_and_reduce(good_for, paths, mermap)

    time3 = time.time()

    run_gmap()
    post_gmap_processing()

    time4 = time.time()

    log.info("[RUNTIME] for graph construction and reduction: {0}".format(time2-time1))
    log.info("[RUNTIME] for path finding and LP solving: {0}".format(time3-time2))
    log.info("[RUNTIME] for GMAP and post-processing: {0}".format(time4-time3))
    log.info("[RUNTIME] Total time in run_Cogent: {0}".format(time4-time1))




def main():
    assert os.path.exists('in.fa')
    assert os.path.exists('in.weights')

    sanity_checks.sanity_check_fasta('in.fa')

    num_size = int(os.popen("grep -c \">\" in.fa").read().strip())

    if num_size <= 20:
        run_Cogent_on_input()
    else:
        dirs = split_files(input_filename='in.fa', split_size=20)
        run_Cogent_on_split_files(dirs)


if __name__ == "__main__":

    from argparse import ArgumentParser
    parser = ArgumentParser()
    parser.add_argument("dirname")
    parser.add_argument("-D", "--gmap_db_path", help="GMAP database location (optional)", default='~/share/gmap_db_new')
    parser.add_argument("-d", "--gmap_species", help="GMAP species name (optional)", default='cuttlefish')

    args = parser.parse_args()

    log = logging.getLogger('Cogent')
    log.setLevel(logging.INFO)

    # create a file handler
    handler = logging.FileHandler(os.path.join(args.dirname, 'hello.log'))
    handler.setLevel(logging.INFO)

    # create a logging format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)

    # add the handlers to the logger
    log.addHandler(handler)

    os.chdir(args.dirname)
    main()
    os.system("touch COGENT.DONE")

    if args.gmap_db_path is not None and args.gmap_species is not None:
        run_gmap_for_final_GFFs(gmap_db_path=args.gmap_db_path, species_db=args.gmap_species)
