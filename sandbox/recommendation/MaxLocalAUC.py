
import numpy 
import logging
import multiprocessing 
import sppy 
import time
import scipy.sparse
from math import exp
from sandbox.util.SparseUtils import SparseUtils
from sandbox.util.SparseUtilsCython import SparseUtilsCython
from sandbox.recommendation.MaxLocalAUCCython import derivativeUi, derivativeVi, updateUVApprox, objectiveApprox, localAUCApprox, updateV, updateU
from sandbox.util.Sampling import Sampling 
from sandbox.util.Util import Util 
from sandbox.data.Standardiser import Standardiser 
from sandbox.util.MCEvaluator import MCEvaluator 


def computeObjective(args): 
    #numpy.random.seed(21)
    #numAucSamples = 100
    
    X, omegaList, U, V, maxLocalAuc  = args 
    U, V, objs, trainAucs, testAucs, iterations, totalTime = maxLocalAuc.learnModel(X, U=U, V=V, verbose=True)
    muObj = numpy.average(objs, weights=numpy.flipud(1/numpy.arange(1, len(objs)+1)))
    #r = SparseUtilsCython.computeR(U, V, maxLocalAuc.w, numAucSamples)
    #objective = objectiveApprox(X, U, V, omegaList, numAucSamples, maxLocalAuc.getLambda(X), r)
    
    logging.debug("Weighted objective: " + str(muObj) + " with t0 = " + str(maxLocalAuc.t0) + " and alpha= " + str(maxLocalAuc.alpha))
    return muObj
    
def computeLocalAuc(args): 
    trainX, testX, testOmegaList, maxLocalAuc  = args 
    
    (m, n) = trainX.shape
    U = numpy.random.rand(m, maxLocalAuc.k)
    V = numpy.random.rand(n, maxLocalAuc.k)                
           

    U, V = maxLocalAuc.learnModel(trainX, U=U, V=V)
    
    r = SparseUtilsCython.computeR(U, V, maxLocalAuc.w, maxLocalAuc.numAucSamples)
    localAuc = localAUCApprox(testX, U, V, testOmegaList, maxLocalAuc.numAucSamples, r) 
    logging.debug("Local AUC: " + str(localAuc) + " with k = " + str(maxLocalAuc.k))
        
    return localAuc
      
class MaxLocalAUC(object): 
    def __init__(self, k, w, sigma=0.05, eps=0.01, stochastic=False, numProcesses=None): 
        """
        Create an object for  maximising the local AUC with a penalty term using the matrix
        decomposition UV.T 
                
        :param k: The rank of matrices U and V
        
        :param w: The quantile for the local AUC - e.g. 1 means takes the largest value, 0.7 means take the top 0.3 
        
        :param sigma: The learning rate 
        
        :param eps: The termination threshold for ||dU|| and ||dV||
        
        :stochastic: Whether to use stochastic gradient descent or gradient descent 
        """
        self.k = k 
        self.w = w
        self.sigma = sigma
        self.eps = eps 
        self.stochastic = stochastic

        if numProcesses == None: 
            self.numProcesses = multiprocessing.cpu_count()

        self.chunkSize = 1        
        
        #Optimal rate doesn't seem to work 
        self.rate = "constant"
        self.alpha = sigma #Initial learning rate 
        self.t0 = 0.1 #Convergence speed - larger means we get to 0 faster
        
        self.nu = 20.0 
        self.project = True
        
        self.recordStep = 20
        self.numRowSamples = 100
        self.numStepIterations = 50
        self.numAucSamples = 100
        self.numRecordAucSamples = 50
        self.maxIterations = 1000
        self.initialAlg = "rand"
        
        #Model selection parameters 
        self.folds = 3 
        self.ks = numpy.array([10, 20, 50, 100])

        #Learning rate selection 
        #self.alphas = numpy.logspace(-2, 1, 10, base=10)
        self.alphas = numpy.logspace(0, 1, 4, base=10)
        self.t0s = numpy.logspace(-10, -1, 4, base=10)
    
    def learnModel(self, X, verbose=False, U=None, V=None, testX=None): 
        """
        Max local AUC with Frobenius norm penalty. Solve with gradient descent. 
        The input is a sparse array. 
        """
        
        m = X.shape[0]
        n = X.shape[1]
        omegaList = SparseUtils.getOmegaList(X)
        if testX != None: 
            testOmegaList = SparseUtils.getOmegaList(testX)

        if U==None or V==None:
            U, V = self.initUV(X)
        
        lastU = numpy.random.rand(m, self.k)
        lastV = numpy.random.rand(n, self.k)
        lastMuObj = 0
        muObj = -1
        
        normDeltaU = numpy.linalg.norm(U - lastU)
        normDeltaV = numpy.linalg.norm(V - lastV)
        objs = []
        trainAucs = []
        testAucs = []
        
        ind = 0
        
        #Convert to a csarray for faster access 
        if scipy.sparse.issparse(X):
            logging.debug("Converting to csarray")
            X2 = sppy.csarray(X, storagetype="row")
            X = X2
        
        #Set up order of indices for stochastic methods 
        if self.stochastic: 
            rowInds = numpy.array(numpy.random.permutation(m), numpy.uint32)
            colInds = numpy.array(numpy.random.permutation(n), numpy.uint32)
        
        startTime = time.time()
    
        while (abs(muObj - lastMuObj) > self.eps) and ind < self.maxIterations:             
            if self.rate == "constant": 
                pass
            elif self.rate == "optimal":
                self.sigma = self.alpha/((1 + self.alpha*self.t0*ind))
            else: 
                raise ValueError("Invalid rate: " + self.rate)
            
            if ind % self.recordStep == 0: 
                r = SparseUtilsCython.computeR(U, V, self.w, self.numRecordAucSamples)
                objs.append(objectiveApprox(X, U, V, omegaList, self.numRecordAucSamples, r))
                trainAucs.append(localAUCApprox(X, U, V, omegaList, self.numRecordAucSamples, r))
                
                if testX != None: 
                    testAucs.append(localAUCApprox(testX, U, V, testOmegaList, self.numAucSamples, r))
                printStr = "Iteration: " + str(ind)
                printStr += " local AUC~" + str(trainAucs[-1]) + " objective~" + str(objs[-1])
                printStr += " sigma=" + str(self.sigma)
                logging.debug(printStr)

            lastMuObj = muObj
            muObj = numpy.average(objs, weights=numpy.flipud(1/numpy.arange(1, len(objs)+1)))
            
            lastU = U.copy() 
            lastV = V.copy()
            
            U  = numpy.ascontiguousarray(U)
            
            self.updateUV(X, U, V, lastU, lastV, rowInds, colInds, ind, omegaList)                          
                            
            if self.stochastic: 
                ind += self.numStepIterations
            else: 
                ind += 1
            
        totalTime = time.time() - startTime
        logging.debug("||dU||=" + str(normDeltaU) + " " + "||dV||=" + str(normDeltaV))
        logging.debug("Total time taken " + str(totalTime))
        logging.debug("Number of iterations: " + str(ind))
                  
        self.U = U 
        self.V = V                  
                  
        if verbose:     
            return U, V, numpy.array(objs), numpy.array(trainAucs), numpy.array(testAucs), ind, totalTime
        else: 
            return U, V
      
    def predict(self, maxItems): 
        return MCEvaluator.recommendAtk(self.U, self.V, maxItems)
          
    def initUV(self, X): 
        m = X.shape[0]
        n = X.shape[1]        
        
        if self.initialAlg == "rand": 
            U = numpy.random.rand(m, self.k)
            V = numpy.random.rand(n, self.k)
        elif self.initialAlg == "svd":
            logging.debug("Initialising with SVD")
            try: 
                U, s, V = SparseUtils.svdPropack(X, self.k, kmax=numpy.min([self.k*15, m-1, n-1]))
            except ImportError: 
                U, s, V = SparseUtils.svdArpack(X, self.k)
            U = numpy.ascontiguousarray(U)
            V = numpy.ascontiguousarray(V)
        else:
            raise ValueError("Unknown initialisation: " + str(self.initialAlg))  
            
        U = Standardiser().normaliseArray(U.T).T    
        V = Standardiser().normaliseArray(V.T).T 
        
        return U, V
        
    def updateUV(self, X, U, V, lastU, lastV, rowInds, colInds, ind, omegaList): 
        """
        Find the derivative with respect to V or part of it. 
        """
        if not self.stochastic:                 
            #r = self.computeConstantR(X)
            r = SparseUtilsCython.computeR(U, V, self.w, self.numAucSamples)
            updateU(X, U, V, omegaList, self.sigma, r, self.nu, self.project)
            updateV(X, U, V, omegaList, self.sigma, r, self.nu, self.project)
        else: 
            updateUVApprox(X, U, V, omegaList, rowInds, colInds, ind, self.sigma, self.numStepIterations, self.numRowSamples, self.numAucSamples, self.w, self.nu, self.project)
    
    def computeConstantR(self, X): 
        return numpy.ones(X.shape[0])*(1-2*self.w)
    
    #@profile
    def derivativeUi(self, X, U, V, omegaList, i, r): 
        """
        delta phi/delta u_i
        """
        return derivativeUi(X, U, V, omegaList, i, r, self.nu)
        
    def derivativeVi(self, X, U, V, omegaList, i, r): 
        """
        delta phi/delta v_i
        """
        return derivativeVi(X, U, V, omegaList, i, r, self.nu)           

    #@profile
    def objective(self, X, U, V, omegaList, r):         
        obj = 0 
        m = X.shape[0]
        
        allInds = numpy.arange(X.shape[1])        
        
        for i in range(X.shape[0]): 
            omegai = omegaList[i]
            omegaBari = numpy.setdiff1d(allInds, omegai, assume_unique=True)
            
            ui = U[i, :]       
            uiV = ui.dot(V.T)
            ri = r[i]
            
            if omegai.shape[0] * omegaBari.shape[0] != 0: 
                partialAuc = 0                
                
                for p in omegai: 
                    uivp = uiV[p]
                    kappa = numpy.exp(-uivp+ri)
                    onePlusKappa = 1+kappa
                    
                    for q in omegaBari: 
                        uivq = uiV[q]
                        gamma = exp(-uivp+uivq)

                        partialAuc += 1/((1+gamma) * onePlusKappa)
                            
                obj += partialAuc/float(omegai.shape[0] * omegaBari.shape[0])
        
        obj /= m       
        obj = - obj
        
        return obj 

    #@profile
    def objectiveApprox(self, X, U, V, omegaList):         
        obj = 0 
        m = X.shape[0]
        
        allInds = numpy.arange(X.shape[1])        
        
        for i in range(X.shape[0]): 
            omegai = omegaList[i]
            omegaBari = numpy.setdiff1d(allInds, omegai, assume_unique=True)
            
            ui = U[i, :]       
            uiV = ui.dot(V.T)
            ri = self.r[i]
            
            if omegai.shape[0] * omegaBari.shape[0] != 0: 
                partialAuc = 0                
                
                indsP = numpy.random.randint(0, omegai.shape[0], self.numAucSamples)  
                indsQ = numpy.random.randint(0, omegaBari.shape[0], self.numAucSamples)
                
                for j in range(self.numAucSamples):                    
                    p = omegai[indsP[j]] 
                    q = omegaBari[indsQ[j]]                  
                
                    uivp = uiV[p]
                    kappa = exp(-uivp+ri)
                    
                    uivq = uiV[q]
                    gamma = exp(-uivp+uivq)

                    partialAuc += 1/((1+gamma) * 1+kappa)
                            
                obj += partialAuc/float(self.numAucSamples)
        
        obj /= m       
        obj = - obj
        
        return obj 
        
    def learningRateSelect(self, X): 
        """
        Let's set the initial learning rate. 
        """        
        m, n = X.shape
        omegaList = SparseUtils.getOmegaList(X)
        objectives = numpy.zeros((self.t0s.shape[0], self.alphas.shape[0]))
        
        paramList = []   
        
        if self.initialAlg != "svd": 
            numInitalUVs = self.folds
        else: 
            numInitalUVs = 1
            
        
        for k in range(numInitalUVs):
            U, V = self.initUV(X)
                        
            for i, t0 in enumerate(self.t0s): 
                for j, alpha in enumerate(self.alphas): 
                    maxLocalAuc = self.copy()
                    maxLocalAuc.t0 = t0
                    maxLocalAuc.alpha = alpha 
                    paramList.append((X, omegaList, U, V, maxLocalAuc))
                    
        pool = multiprocessing.Pool(processes=self.numProcesses, maxtasksperchild=100)
        resultsIterator = pool.imap(computeObjective, paramList, self.chunkSize)
        #import itertools
        #resultsIterator = itertools.imap(computeObjective, paramList)
        
        for k in range(numInitalUVs):
            for i, t0 in enumerate(self.t0s): 
                for j, alpha in enumerate(self.alphas):  
                    objectives[i, j] += resultsIterator.next()
            
        pool.terminate()
        objectives /= numInitalUVs    
        logging.debug(objectives)
        
        t0 = self.t0s[numpy.unravel_index(numpy.argmin(objectives), objectives.shape)[0]]
        alpha = self.alphas[numpy.unravel_index(numpy.argmin(objectives), objectives.shape)[1]]
        
        logging.debug("Learning rate parameters: t0=" + str(t0) + " alpha=" + str(alpha))
        
        self.t0 = t0 
        self.alpha = alpha 
        
        return objectives
        
    def modelSelect(self, X): 
        """
        Perform model selection on X and return the best parameters. 
        """
        m, n = X.shape
        cvInds = Sampling.randCrossValidation(self.folds, X.nnz)
        localAucs = numpy.zeros((self.ks.shape[0], len(cvInds)))
        
        logging.debug("Performing model selection")
        paramList = []        
        
        for icv, (trainInds, testInds) in enumerate(cvInds):
            Util.printIteration(icv, 1, self.folds, "Fold: ")

            trainX = SparseUtils.submatrix(X, trainInds)
            testX = SparseUtils.submatrix(X, testInds)
            
            testOmegaList = SparseUtils.getOmegaList(testX)
            
            for i, k in enumerate(self.ks): 
                maxLocalAuc = self.copy()
                maxLocalAuc.k = k
                paramList.append((trainX, testX, testOmegaList, maxLocalAuc))
                    
        pool = multiprocessing.Pool(processes=self.numProcesses, maxtasksperchild=100)
        resultsIterator = pool.imap(computeLocalAuc, paramList, self.chunkSize)
        #import itertools
        #resultsIterator = itertools.imap(localAucsRhos, paramList)
        
        for icv, (trainInds, testInds) in enumerate(cvInds):        
            for i, k in enumerate(self.ks): 
                tempAuc = resultsIterator.next()
                localAucs[i, icv] = tempAuc
        
        pool.terminate()
        
        meanLocalAucs = numpy.mean(localAucs, 1)
        stdLocalAucs = numpy.std(localAucs, 1)
        
        logging.debug(meanLocalAucs)
        
        k = self.ks[numpy.argmax(meanLocalAucs)]
        
        logging.debug("Model parameters: k=" + str(k))
        
        self.k = k 
        
        return meanLocalAucs, stdLocalAucs
    
    def __str__(self): 
        outputStr = "MaxLocalAUC: k=" + str(self.k) + " sigma=" + str(self.sigma) + " eps=" + str(self.eps) 
        outputStr += " stochastic=" + str(self.stochastic) + " numRowSamples=" + str(self.numRowSamples) + " numStepIterations=" + str(self.numStepIterations)
        outputStr += " numAucSamples=" + str(self.numAucSamples) + " maxIterations=" + str(self.maxIterations) + " initialAlg=" + self.initialAlg
        outputStr += " w=" + str(self.w) + " rate=" + str(self.rate) + " alpha=" + str(self.alpha) + " t0=" + str(self.t0) + " folds=" + str(self.folds)
        outputStr += " nu=" + str(self.nu)
        
        return outputStr 

    def copy(self): 
        maxLocalAuc = MaxLocalAUC(k=self.k, w=self.w)
        maxLocalAuc.sigma = self.sigma
        maxLocalAuc.eps = self.eps 
        maxLocalAuc.stochastic = self.stochastic
     
        maxLocalAuc.rate = self.rate
        maxLocalAuc.alpha = self.alpha
        maxLocalAuc.t0 = self.t0
        
        maxLocalAuc.recordStep = self.recordStep
        maxLocalAuc.numRowSamples = self.numRowSamples
        maxLocalAuc.numStepIterations = self.numStepIterations
        maxLocalAuc.numAucSamples = self.numAucSamples
        maxLocalAuc.maxIterations = self.maxIterations
        maxLocalAuc.initialAlg = self.initialAlg
        
        maxLocalAuc.ks = self.ks
        maxLocalAuc.folds = self.folds
        
        return maxLocalAuc
        