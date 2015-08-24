from collections import defaultdict
from os.path import join, exists
from os import makedirs, unlink
from itertools import chain

from sqlalchemy import (text, column, bindparam)
from gensim import corpora, models as gmodels
from gensim.utils import tokenize
import numpy
from scipy.sparse import lil_matrix
import sklearn.cluster
from sklearn import metrics


from assembl.models import Content, Idea, Discussion
from .indexedcorpus import IdMmCorpus
from . import (
    locale_to_lang, get_stop_words, known_languages, get_stemmer,
    DummyStemmer, ReversibleStemmer)

nlp_data = 'var/nlp'


def as_word_list(post, stemmer, stop_words):
    text = (post.subject or "") + " " + post.get_body_as_text()
    return [stemmer.stemWord(word)
            for word in tokenize(text, True)
            if word not in stop_words]


def create_dictionaries():
    db = Discussion.default_db
    by_main_lang = defaultdict(list)
    for d in db.query(Discussion).all():
        main_lang = d.discussion_locales[0].split('_')[0]
        by_main_lang[main_lang].append(d.id)
    for lang, discussion_ids in by_main_lang.iteritems():
        dirname = join(nlp_data, lang)
        if not exists(dirname):
            makedirs(dirname)
        stemmer = get_stemmer(lang)
        # Actually it might be better to go phrases (longer),
        # then stop words filtering, then stemming
        # on non-phrase. BUT phrases may require stemming too.
        if not isinstance(stemmer, DummyStemmer):
            stemmer = ReversibleStemmer(
                stemmer, join(dirname, 'stems.dict'))
        stop_words = get_stop_words(lang)
        posts = db.query(Content).join(Discussion).filter(
            Discussion.id.in_(discussion_ids))
        phrases = gmodels.Phrases()
        phrases.add_vocab((
            as_word_list(post, stemmer, stop_words) for post in posts))

        dictionary = corpora.Dictionary((
            phrases[as_word_list(post, stemmer, stop_words)]
            for post in posts))
        dictionary.save(join(dirname, 'dico.dict'))
        IdMmCorpus.serialize(join(dirname, 'posts.mm'), (
            (post.id, dictionary.doc2bow(
                phrases[as_word_list(post, stemmer, stop_words)]))
            for post in posts))
        if not isinstance(stemmer, DummyStemmer):
            stemmer.save()


def gensimvecs_to_csr(vecs, width):
    model_matrix = lil_matrix(
        (len(vecs), width), dtype=numpy.float64)
    for nrow, row in enumerate(vecs):
        for ncol, val in row:
            model_matrix[nrow, ncol] = val
    return model_matrix.tocsr()


def get_discussion_semantic_analysis(
        discussion_id, num_topics=200,
        model_cls=gmodels.ldamodel.LdaModel, **model_kwargs):
    discussion = Discussion.get(discussion_id)
    lang = discussion.discussion_locales[0].split('_')[0]
    dirname = join(nlp_data, lang)
    dictionary = corpora.Dictionary.load(join(dirname, 'dico.dict'))
    post_ids = discussion.db.query(Content.id).filter_by(
        discussion_id=discussion_id)
    doc_count = post_ids.count()
    if doc_count < 10:
        return None, None
    tfidf_model = gmodels.TfidfModel(id2word=dictionary)
    tfidf_fname = join(dirname, "tfidf_%d.model" % (discussion_id,))
    model_fname = join(dirname, "model_%s_%d.model" % (
        model_cls.__name__, discussion_id,))
    gensim_model = model_cls(
        id2word=dictionary, num_topics=num_topics, **model_kwargs)
    if exists(tfidf_fname):
        tfidf_model = tfidf_model.load(tfidf_fname)
        # assumption: count implies identity.
        # Wrong in corner cases: hidden, etc.
        if tfidf_model.num_docs == doc_count:
            if exists(model_fname):
                gensim_model = gensim_model.load(model_fname)
                same_kwargs = all((
                    getattr(gensim_model, k) == v
                    for (k, v) in model_kwargs.iteritems()))
                same_kwargs = same_kwargs and getattr(
                    gensim_model, 'num_updates', doc_count) == doc_count
                if same_kwargs and gensim_model.num_topics == num_topics:
                    return tfidf_model, gensim_model
        elif exists(tfidf_fname):
            unlink(tfidf_fname)
    if exists(model_fname):
        unlink(model_fname)
    post_ids = [x for (x,) in post_ids]
    corpus = IdMmCorpus(join(dirname, 'posts.mm'))
    subcorpus = corpus.subcorpus(post_ids)
    if tfidf_model.num_docs != doc_count:
        tfidf_model.initialize(subcorpus)
        tfidf_model.save(tfidf_fname)
    tfidf_corpus = tfidf_model[subcorpus]
    if getattr(gensim_model, 'update', None):
        gensim_model.update(tfidf_corpus)
    elif getattr(gensim_model, 'add_documents', None):
        gensim_model.add_documents(tfidf_corpus)
    gensim_model.save(model_fname)
    return (tfidf_model, gensim_model)


def identity(x):
    return x


def parse_topic(topic, trans=identity):
    words = topic.split(' + ')
    words = (word.split('*') for word in words)
    return dict(((' '.join((
            trans(w) for w in k.strip('"').split('_') if w)), float(v))
        for (v, k) in words))


def post_ids_of(idea):
    related = text(
        Idea._get_related_posts_statement(),
        bindparams=[bindparam('root_idea_id', idea.id),
                    bindparam('discussion_id', idea.discussion_id)]
        ).columns(column('post_id')).alias('related')
    post_ids = idea.db.query(Content.id).join(
        related, Content.id == related.c.post_id)
    return [x for (x,) in post_ids]


def get_cluster_info(
        idea_id, num_topics=200, passes=5,
        algorithm="DBSCAN", **algo_kwargs):
    idea = Idea.get(idea_id)
    tfidf_model, gensim_model = get_discussion_semantic_analysis(
        idea.discussion_id, num_topics=num_topics, passes=passes)
    # , model_cls=gmodels.lsimodel.LsiModel
    if not tfidf_model or not gensim_model:
        return
    lang = idea.discussion.discussion_locales[0].split('_')[0]
    dirname = join(nlp_data, lang)
    stemmer = get_stemmer(lang)
    trans = identity
    if not isinstance(stemmer, DummyStemmer):
        stemmer = ReversibleStemmer(
            stemmer, join(dirname, 'stems.dict'))

        def trans(x):
            return stemmer.reverse.get(x, x)
    corpus = IdMmCorpus(join(dirname, 'posts.mm'))
    post_ids = post_ids_of(idea)
    if len(post_ids) < 10:
        return
    post_id_by_index = {n: post_id for (n, post_id) in enumerate(post_ids)}
    subcorpus = corpus.subcorpus(post_ids)
    tfidf_corpus = tfidf_model[subcorpus]
    model_matrix = gensimvecs_to_csr(gensim_model[tfidf_corpus], num_topics)
    algorithm = getattr(sklearn.cluster, algorithm)
    algorithm = algorithm(**algo_kwargs)
    r = algorithm.fit(model_matrix)
    labels = r.labels_
    n_clusters_raw = len(set(labels))
    # n_clusters_ = n_clusters_raw - (1 if -1 in labels else 0)
    silhouette_score = None
    if n_clusters_raw > 1:
        silhouette_score = metrics.silhouette_score(model_matrix, labels)
    post_clusters = []
    remainder = set(post_ids)
    for label in set(labels):
        if label == -1:
            continue
        subset = [n for (n, l) in enumerate(labels) if label == l]
        cluster = [post_id_by_index[n] for n in subset]
        remainder -= set(cluster)
        post_clusters.append(cluster)
    remainder = list(remainder)
    all_cluster_features = []
    for cluster in post_clusters:
        cluster_corpus = corpus.subcorpus(cluster)
        clusterneg_corpus = corpus.subcorpus(
            list(set(post_ids) - set(cluster)))

        def centroid(corpus):
            clust_lda = [gensim_model[tfidf_model[c]] for c in corpus]
            clust_lda = gensimvecs_to_csr(clust_lda, num_topics)
            return clust_lda.sum(0).A1 / clust_lda.shape[0]

        difference_vals = centroid(cluster_corpus) - centroid(clusterneg_corpus)
        difference = difference_vals.argsort()
        extremes = defaultdict(float)
        for id in chain(difference[0:5], difference[-1:-6:-1]):
            factor = difference_vals[id]
            terms = parse_topic(gensim_model.print_topic(id), trans)
            for term, val in terms.iteritems():
                extremes[term] += val * factor
        extremes = [(val, term) for (term, val) in extremes.iteritems()]
        extremes.sort()
        pos_terms = [t for (v, t) in extremes if v > 0][0:15]
        neg_terms = [t for (v, t) in extremes if v < 0][0:15]
        pos_terms.reverse()
        all_cluster_features.append((pos_terms, neg_terms))
    # Compare to children classification
    compare_with_ideas = None
    if len(idea.children):
        children_remainder = set(post_ids)
        posts_of_children = {
            child.id: post_ids_of(child)
            for child in idea.children}
        ideas_of_post = defaultdict(list)
        for idea_id, c_post_ids in posts_of_children.iteritems():
            for post_id in c_post_ids:
                ideas_of_post[post_id].append(idea_id)
            children_remainder -= set(c_post_ids)
        for post_id in children_remainder:
            ideas_of_post[post_id] = [idea_id]
        # if many ideas to a post, choose one with the most ideas in same cluster.
        # A bit arbitrary but I need a single idea.
        for cluster in chain(post_clusters, (remainder,)):
            idea_score = defaultdict(int)
            for post_id in cluster:
                for idea_id in ideas_of_post[post_id]:
                    idea_score[idea_id] += 1
            for post_id in cluster:
                if len(ideas_of_post[post_id]) > 1:
                    scores = [(idea_score[idea_id], idea_id)
                              for idea_id in ideas_of_post[post_id]]
                    scores.sort()
                    ideas_of_post[post_id] = [scores[-1][1]]
        # index_by_post_id = {v: k for (k, v) in post_id_by_index.iteritems()}
        idea_of_index = [ideas_of_post[post_id][0] for post_id in post_ids]
        compare_with_ideas = {
            "Homogeneity": metrics.homogeneity_score(idea_of_index, labels),
            "Completeness": metrics.completeness_score(idea_of_index, labels),
            "V-measure": metrics.v_measure_score(idea_of_index, labels),
            "Adjusted Rand Index": metrics.adjusted_rand_score(
                idea_of_index, labels),
            "Adjusted Mutual Information": metrics.adjusted_mutual_info_score(
                idea_of_index, labels)}

    return (silhouette_score, compare_with_ideas, post_clusters,
            remainder, all_cluster_features)


def show_clusters(clusters):
    posts = Content.default_db.query(Content).filter(
        Content.id.in_(list(chain(*clusters)))).all()
    posts = {p.id: p for p in posts}
    for n, cluster in enumerate(clusters):
        print "*"*100, "Cluster", n+1
        for post_id in cluster:
            print posts[post_id].get_body_as_text()


def show_all(db, discussion_id, eps=0.2, min_samples=4):
    idea_ids = db.query(Idea.id).filter_by(
        discussion_id=discussion_id).all()
    results = {id: get_cluster_info(id, eps=eps, min_samples=min_samples)
               for (id,) in idea_ids}
    posres = {id: r for (id, r) in results.iteritems() if r is not None}
    for id, (silh, compare, clusters, rem, topics) in posres.iteritems():
        print id, silh, [len(x) for x in clusters], len(rem)