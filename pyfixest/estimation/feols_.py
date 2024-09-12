import functools
import gc
import warnings
from importlib import import_module
from typing import Optional, Union

import numba as nb
import numpy as np
import pandas as pd
import polars as pl
from formulaic import Formula
from scipy.sparse.linalg import lsqr
from scipy.stats import chi2, f, norm, t

from pyfixest.errors import VcovTypeNotSupportedError
from pyfixest.estimation.ritest import (
    _decode_resampvar,
    _get_ritest_pvalue,
    _get_ritest_stats_fast,
    _get_ritest_stats_slow,
    _plot_ritest_pvalue,
)
from pyfixest.estimation.vcov_utils import (
    _check_cluster_df,
    _compute_bread,
    _count_G_for_ssc_correction,
    _crv1_meat_loop,
    _get_cluster_df,
    _prepare_twoway_clustering,
)
from pyfixest.utils.dev_utils import (
    DataFrameType,
    _extract_variable_level,
    _polars_to_pandas,
    _select_order_coefs,
)
from pyfixest.utils.utils import get_ssc, simultaneous_crit_val


class Feols:
    """
    Non user-facing class to estimate a liner regression via OLS.

    Users should not directly instantiate this class,
    but rather use the [feols()](/reference/estimation.feols.qmd) function. Note that
    no demeaning is performed in this class: demeaning is performed in the
    [FixestMulti](/reference/estimation.fixest_multi.qmd) class (to allow for caching
    of demeaned variables for multiple estimation).

    Parameters
    ----------
    Y : np.ndarray
        Dependent variable, a two-dimensional numpy array.
    X : np.ndarray
        Independent variables, a two-dimensional numpy array.
    weights : np.ndarray
        Weights, a one-dimensional numpy array.
    collin_tol : float
        Tolerance level for collinearity checks.
    coefnames : list[str]
        Names of the coefficients (of the design matrix X).
    weights_name : Optional[str]
        Name of the weights variable.
    weights_type : Optional[str]
        Type of the weights variable. Either "aweights" for analytic weights or
        "fweights" for frequency weights.
    solver : str, optional.
        The solver to use for the regression. Can be either "np.linalg.solve" or
        "np.linalg.lstsq". Defaults to "np.linalg.solve".

    Attributes
    ----------
    _method : str
        Specifies the method used for regression, set to "feols".
    _is_iv : bool
        Indicates whether instrumental variables are used, initialized as False.

    _Y : np.ndarray
        The dependent variable array.
    _X : np.ndarray
        The independent variables array.
    _X_is_empty : bool
        Indicates whether the X array is empty.
    _collin_tol : float
        Tolerance level for collinearity checks.
    _coefnames : list
        Names of the coefficients (of the design matrix X).
    _collin_vars : list
        Variables identified as collinear.
    _collin_index : list
        Indices of collinear variables.
    _Z : np.ndarray
        Alias for the _X array, used for calculations.
    _solver: str
        The solver used for the regression.
    _weights : np.ndarray
        Array of weights for each observation.
    _N : int
        Number of observations.
    _k : int
        Number of independent variables (or features).
    _support_crv3_inference : bool
        Indicates support for CRV3 inference.
    _data : Any
        Data used in the regression, to be enriched outside of the class.
    _fml : Any
        Formula used in the regression, to be enriched outside of the class.
    _has_fixef : bool
        Indicates whether fixed effects are used.
    _fixef : Any
        Fixed effects used in the regression.
    _icovars : Any
        Internal covariates, to be enriched outside of the class.
    _ssc_dict : dict
        dictionary for sum of squares and cross products matrices.
    _tZX : np.ndarray
        Transpose of Z multiplied by X, set in get_fit().
    _tXZ : np.ndarray
        Transpose of X multiplied by Z, set in get_fit().
    _tZy : np.ndarray
        Transpose of Z multiplied by Y, set in get_fit().
    _tZZinv : np.ndarray
        Inverse of the transpose of Z multiplied by Z, set in get_fit().
    _beta_hat : np.ndarray
        Estimated regression coefficients.
    _Y_hat_link : np.ndarray
        Predicted values of the dependent variable.
    _Y_hat_response : np.ndarray
        Response predictions of the model.
    _u_hat : np.ndarray
        Residuals of the regression model.
    _scores : np.ndarray
        Scores used in the regression analysis.
    _hessian : np.ndarray
        Hessian matrix used in the regression.
    _bread : np.ndarray
        Bread matrix, used in calculating the variance-covariance matrix.
    _vcov_type : Any
        Type of variance-covariance matrix used.
    _vcov_type_detail : Any
        Detailed specification of the variance-covariance matrix type.
    _is_clustered : bool
        Indicates if clustering is used in the variance-covariance calculation.
    _clustervar : Any
        Variable used for clustering in the variance-covariance calculation.
    _G : Any
        Group information used in clustering.
    _ssc : Any
        Sum of squares and cross products matrix.
    _vcov : np.ndarray
        Variance-covariance matrix of the estimated coefficients.
    _se : np.ndarray
        Standard errors of the estimated coefficients.
    _tstat : np.ndarray
        T-statistics of the estimated coefficients.
    _pvalue : np.ndarray
        P-values associated with the t-statistics.
    _conf_int : np.ndarray
        Confidence intervals for the estimated coefficients.
    _F_stat : Any
        F-statistic for the model, set in get_Ftest().
    _fixef_dict : dict
        dictionary containing fixed effects estimates.
    _sumFE : np.ndarray
        Sum of all fixed effects for each observation.
    _rmse : float
        Root mean squared error of the model.
    _r2 : float
        R-squared value of the model.
    _r2_within : float
        R-squared value computed on demeaned dependent variable.
    _adj_r2 : float
        Adjusted R-squared value of the model.
    _adj_r2_within : float
        Adjusted R-squared value computed on demeaned dependent variable.
    _solver: str
        The solver used to fit the normal equation.
    """

    def __init__(
        self,
        Y: np.ndarray,
        X: np.ndarray,
        weights: np.ndarray,
        collin_tol: float,
        coefnames: list[str],
        weights_name: Optional[str],
        weights_type: Optional[str],
        solver: str = "np.linalg.solve",
    ) -> None:
        self._method = "feols"
        self._is_iv = False

        self._weights = weights
        self._weights_name = weights_name
        self._weights_type = weights_type
        self._has_weights = False
        if weights_name is not None:
            self._has_weights = True

        if self._has_weights:
            w = np.sqrt(weights)
            self._Y = Y * w
            self._X = X * w
        else:
            self._Y = Y
            self._X = X

        self.get_nobs()
        self._solver = solver
        _feols_input_checks(Y, X, weights)

        if self._X.shape[1] == 0:
            self._X_is_empty = True
        else:
            self._X_is_empty = False
            self._collin_tol = collin_tol
            (
                self._X,
                self._coefnames,
                self._collin_vars,
                self._collin_index,
            ) = _drop_multicollinear_variables(self._X, coefnames, self._collin_tol)

        self._Z = self._X

        _, self._k = self._X.shape

        self._support_crv3_inference = True
        if self._weights_name is not None:
            self._supports_wildboottest = False
        self._supports_wildboottest = True
        self._supports_cluster_causal_variance = True
        if self._has_weights or self._is_iv:
            self._supports_wildboottest = False

        # attributes that have to be enriched outside of the class -
        # not really optimal code change later
        self._data = pd.DataFrame()
        self._fml = ""
        self._has_fixef = False
        self._fixef = ""
        # self._coefnames = None
        self._icovars = None
        self._ssc_dict: dict[str, Union[str, bool]] = {}

        # set in get_fit()
        self._tZX = np.array([])
        # self._tZXinv = None
        self._tXZ = np.array([])
        self._tZy = np.array([])
        self._tZZinv = np.array([])
        self._beta_hat = np.array([])
        self._Y_hat_link = np.array([])
        self._Y_hat_response = np.array([])
        self._u_hat = np.array([])
        self._scores = np.array([])
        self._hessian = np.array([])
        self._bread = np.array([])

        # set in vcov()
        self._vcov_type = ""
        self._vcov_type_detail = ""
        self._is_clustered = False
        self._clustervar: list[str] = []
        self._G: list[int] = []
        self._ssc = np.array([], dtype=np.float64)
        self._vcov = np.array([])
        self.na_index = np.array([])  # initiated outside of the class
        self.n_separation_na = 0

        # set in get_inference()
        self._se = np.array([])
        self._tstat = np.array([])
        self._pvalue = np.array([])
        self._conf_int = np.array([])

        # set in get_Ftest()
        self._F_stat = None

        # set in fixef()
        self._fixef_dict: dict[str, dict[str, float]] = {}
        self._sumFE = None

        # set in get_performance()
        self._rmse = np.nan
        self._r2 = np.nan
        self._r2_within = np.nan
        self._adj_r2 = np.nan
        self._adj_r2_within = np.nan

        # special for poisson
        self.deviance = None

        # set functions inherited from other modules
        _module = import_module("pyfixest.report")
        _tmp = getattr(_module, "coefplot")
        self.coefplot = functools.partial(_tmp, models=[self])
        self.coefplot.__doc__ = _tmp.__doc__
        _tmp = getattr(_module, "iplot")
        self.iplot = functools.partial(_tmp, models=[self])
        self.iplot.__doc__ = _tmp.__doc__
        _tmp = getattr(_module, "summary")
        self.summary = functools.partial(_tmp, models=[self])
        self.summary.__doc__ = _tmp.__doc__

    def solve_ols(self, tZX: np.ndarray, tZY: np.ndarray, solver: str):
        """
        Solve the ordinary least squares problem using the specified solver.

        Parameters
        ----------
        tZX (array-like): Z'X.
        tZY (array-like): Z'Y.
        solver (str): The solver to use. Supported solvers are "np.linalg.lstsq",
        "np.linalg.solve", and "scipy.sparse.linalg.lsqr".

        Returns
        -------
        array-like: The solution to the ordinary least squares problem.

        Raises
        ------
        ValueError: If the specified solver is not supported.
        """
        if solver == "np.linalg.lstsq":
            return np.linalg.lstsq(tZX, tZY, rcond=None)[0].flatten()
        elif solver == "np.linalg.solve":
            return np.linalg.solve(tZX, tZY).flatten()
        elif solver == "scipy.sparse.linalg.lsqr":
            return lsqr(tZX, tZY)[0].flatten()
        else:
            raise ValueError(f"Solver {solver} not supported.")

    def get_fit(self) -> None:
        """
        Fit an OLS model.

        Returns
        -------
        None
        """
        _X = self._X
        _Y = self._Y
        _Z = self._Z
        _solver = self._solver
        self._tZX = _Z.T @ _X
        self._tZy = _Z.T @ _Y

        self._beta_hat = self.solve_ols(self._tZX, self._tZy, _solver)

        self._Y_hat_link = self._X @ self._beta_hat
        self._u_hat = self._Y.flatten() - self._Y_hat_link.flatten()

        self._scores = _X * self._u_hat[:, None]
        self._hessian = self._tZX.copy()

        # IV attributes, set to None for OLS, Poisson
        self._tXZ = np.array([])
        self._tZZinv = np.array([])

    def vcov(
        self, vcov: Union[str, dict[str, str]], data: Optional[DataFrameType] = None
    ) -> "Feols":
        """
        Compute covariance matrices for an estimated regression model.

        Parameters
        ----------
        vcov : Union[str, dict[str, str]]
            A string or dictionary specifying the type of variance-covariance matrix
            to use for inference.
            If a string, it can be one of "iid", "hetero", "HC1", "HC2", "HC3".
            If a dictionary, it should have the format {"CRV1": "clustervar"} for
            CRV1 inference or {"CRV3": "clustervar"}
            for CRV3 inference. Note that CRV3 inference is currently not supported
            for IV estimation.
        data: Optional[DataFrameType], optional
            The data used for estimation. If None, tries to fetch the data from the
            model object. Defaults to None.


        Returns
        -------
        Feols
            An instance of class [Feols(/reference/Feols.qmd) with updated inference.
        """
        # Assuming `data` is the DataFrame in question
        if isinstance(data, pl.DataFrame):
            data = _polars_to_pandas(data)

        _data = self._data
        _has_fixef = self._has_fixef
        _is_iv = self._is_iv
        _method = self._method
        _support_crv3_inference = self._support_crv3_inference

        _tXZ = self._tXZ
        _tZZinv = self._tZZinv
        _tZX = self._tZX
        # _tZXinv = self._tZXinv
        _hessian = self._hessian

        _ssc_dict = self._ssc_dict
        _N = self._N
        _k = self._k

        # deparse vcov input
        _check_vcov_input(vcov, _data)
        (
            self._vcov_type,
            self._vcov_type_detail,
            self._is_clustered,
            self._clustervar,
        ) = _deparse_vcov_input(vcov, _has_fixef, _is_iv)

        self._bread = _compute_bread(_is_iv, _tXZ, _tZZinv, _tZX, _hessian)

        # compute vcov
        if self._vcov_type == "iid":
            self._ssc = get_ssc(
                ssc_dict=_ssc_dict,
                N=_N,
                k=_k,
                G=1,
                vcov_sign=1,
                vcov_type="iid",
            )

            self._vcov = self._ssc * self._vcov_iid()

        elif self._vcov_type == "hetero":
            # this is what fixest does internally: see fixest:::vcov_hetero_internal:
            # adj = ifelse(ssc$cluster.adj, n/(n - 1), 1)

            self._ssc = get_ssc(
                ssc_dict=_ssc_dict,
                N=_N,
                k=_k,
                G=_N,  # all clusters are singletons
                vcov_sign=1,
                vcov_type="hetero",
            )

            self._vcov = self._ssc * self._vcov_hetero()

        elif self._vcov_type == "CRV":
            if data is not None:
                # use input data set
                self._cluster_df = _get_cluster_df(
                    data=data,
                    clustervar=self._clustervar,
                )
                _check_cluster_df(cluster_df=self._cluster_df, data=data)
            else:
                # use stored data
                self._cluster_df = _get_cluster_df(
                    data=self._data, clustervar=self._clustervar
                )
                _check_cluster_df(cluster_df=self._cluster_df, data=self._data)

            if self._cluster_df.shape[1] > 1:
                self._cluster_df = _prepare_twoway_clustering(
                    clustervar=self._clustervar, cluster_df=self._cluster_df
                )

            self._G = _count_G_for_ssc_correction(
                cluster_df=self._cluster_df, ssc_dict=_ssc_dict
            )

            # loop over columns of cluster_df
            vcov_sign_list = [1, 1, -1]
            self._vcov = np.zeros((self._k, self._k))

            for x, col in enumerate(self._cluster_df.columns):
                cluster_col_pd = self._cluster_df[col]
                cluster_col, _ = pd.factorize(cluster_col_pd)
                clustid = np.unique(cluster_col)

                ssc = get_ssc(
                    ssc_dict=_ssc_dict,
                    N=_N,
                    k=_k,
                    G=self._G[x],
                    vcov_sign=vcov_sign_list[x],
                    vcov_type="CRV",
                )

                self._ssc = np.array([ssc]) if x == 0 else np.append(self._ssc, ssc)

                if self._vcov_type_detail == "CRV1":
                    self._vcov += self._ssc[x] * self._vcov_crv1(
                        clustid=clustid, cluster_col=cluster_col
                    )

                elif self._vcov_type_detail == "CRV3":
                    # check: is fixed effect cluster fixed effect?
                    # if not, either error or turn fixefs into dummies
                    # for now: don't allow for use with fixed effects

                    if not _support_crv3_inference:
                        raise VcovTypeNotSupportedError(
                            "CRV3 inference is not supported with IV regression."
                        )

                    if (
                        (_has_fixef is False)
                        and (_method == "feols")
                        and (_is_iv is False)
                    ):
                        self._vcov += self._ssc[x] * self._vcov_crv3_fast(
                            clustid=clustid, cluster_col=cluster_col
                        )
                    else:
                        self._vcov += self._ssc[x] * self._vcov_crv3_slow(
                            clustid=clustid, cluster_col=cluster_col
                        )

        # update p-value, t-stat, standard error, confint
        self.get_inference()

        return self

    def _vcov_iid(self):
        _N = self._N
        _u_hat = self._u_hat
        _method = self._method
        _bread = self._bread

        if _method == "feols":
            sigma2 = np.sum(_u_hat.flatten() ** 2) / (_N - 1)
        elif _method == "fepois":
            sigma2 = 1
        else:
            raise NotImplementedError(
                f"'iid' inference is not supported for {_method} regressions."
            )

        _vcov = _bread * sigma2

        return _vcov

    def _vcov_hetero(self):
        _scores = self._scores
        _vcov_type_detail = self._vcov_type_detail
        _tXZ = self._tXZ
        _tZZinv = self._tZZinv
        _tZX = self._tZX
        _X = self._X
        _is_iv = self._is_iv
        _bread = self._bread

        if _vcov_type_detail in ["hetero", "HC1"]:
            transformed_scores = _scores
        elif _vcov_type_detail in ["HC2", "HC3"]:
            leverage = np.sum(_X * (_X @ np.linalg.inv(_tZX)), axis=1)
            transformed_scores = (
                _scores / np.sqrt(1 - leverage)[:, None]
                if _vcov_type_detail == "HC2"
                else _scores / (1 - leverage)[:, None]
            )

        Omega = transformed_scores.T @ transformed_scores

        _meat = _tXZ @ _tZZinv @ Omega @ _tZZinv @ _tZX if _is_iv else Omega
        _vcov = _bread @ _meat @ _bread

        return _vcov

    def _vcov_crv1(self, clustid, cluster_col):
        _Z = self._Z
        _weights = self._weights
        _u_hat = self._u_hat
        _method = self._method
        _is_iv = self._is_iv
        _tXZ = self._tXZ
        _tZZinv = self._tZZinv
        _tZX = self._tZX
        _bread = self._bread

        k_instruments = _Z.shape[1]
        meat = np.zeros((k_instruments, k_instruments))

        # deviance uniquely for Poisson

        weighted_uhat = _u_hat.reshape(-1, 1) if _u_hat.ndim == 1 else _u_hat

        meat = _crv1_meat_loop(
            _Z=_Z.astype(np.float64),
            weighted_uhat=weighted_uhat.astype(np.float64),
            clustid=clustid,
            cluster_col=cluster_col,
        )

        if _is_iv is False:
            _vcov = _bread @ meat @ _bread
        else:
            meat = _tXZ @ _tZZinv @ meat @ _tZZinv @ _tZX
            _vcov = _bread @ meat @ _bread

        return _vcov

    def _vcov_crv3_fast(self, clustid, cluster_col):
        _k = self._k
        _Y = self._Y
        _X = self._X
        _beta_hat = self._beta_hat

        beta_jack = np.zeros((len(clustid), _k))

        # inverse hessian precomputed?
        tXX = np.transpose(_X) @ _X
        tXy = np.transpose(_X) @ _Y

        # compute leave-one-out regression coefficients (aka clusterjacks')  # noqa: W505
        for ixg, g in enumerate(clustid):
            Xg = _X[np.equal(g, cluster_col)]
            Yg = _Y[np.equal(g, cluster_col)]
            tXgXg = np.transpose(Xg) @ Xg
            # jackknife regression coefficient
            beta_jack[ixg, :] = (
                np.linalg.pinv(tXX - tXgXg) @ (tXy - np.transpose(Xg) @ Yg)
            ).flatten()

        # optional: beta_bar in MNW (2022)
        # center = "estimate"
        # if center == 'estimate':
        #    beta_center = beta_hat
        # else:
        #    beta_center = np.mean(beta_jack, axis = 0)
        beta_center = _beta_hat

        vcov_mat = np.zeros((_k, _k))
        for ixg, g in enumerate(clustid):
            beta_centered = beta_jack[ixg, :] - beta_center
            vcov_mat += np.outer(beta_centered, beta_centered)

        _vcov = vcov_mat

        return _vcov

    def _vcov_crv3_slow(self, clustid, cluster_col):
        _k = self._k
        _method = self._method
        _fml = self._fml
        _data = self._data
        _weights_name = self._weights_name
        _weights_type = self._weights_type
        _beta_hat = self._beta_hat

        beta_jack = np.zeros((len(clustid), _k))

        # lazy loading to avoid circular import
        fixest_module = import_module("pyfixest.estimation")
        if _method == "feols":
            fit_ = getattr(fixest_module, "feols")
        else:
            fit_ = getattr(fixest_module, "fepois")

        for ixg, g in enumerate(clustid):
            # direct leave one cluster out implementation
            data = _data[~np.equal(g, cluster_col)]
            fit = fit_(
                fml=_fml,
                data=data,
                vcov="iid",
                weights=_weights_name,
                weights_type=_weights_type,
            )
            beta_jack[ixg, :] = fit.coef().to_numpy()

        # optional: beta_bar in MNW (2022)
        # center = "estimate"
        # if center == 'estimate':
        #    beta_center = beta_hat
        # else:
        #    beta_center = np.mean(beta_jack, axis = 0)
        beta_center = _beta_hat

        vcov_mat = np.zeros((_k, _k))
        for ixg, g in enumerate(clustid):
            beta_centered = beta_jack[ixg, :] - beta_center
            vcov_mat += np.outer(beta_centered, beta_centered)

        _vcov = vcov_mat

        return _vcov

    def get_inference(self, alpha: float = 0.05) -> None:
        """
        Compute standard errors, t-statistics, and p-values for the regression model.

        Parameters
        ----------
        alpha : float, optional
            The significance level for confidence intervals. Defaults to 0.05, which
            produces a 95% confidence interval.

        Returns
        -------
        None
        """
        _vcov = self._vcov
        _beta_hat = self._beta_hat
        _vcov_type = self._vcov_type
        _N = self._N
        _k = self._k
        _G = (
            np.min(np.array(self._G)) if self._vcov_type == "CRV" else np.array(self._G)
        )  # fixest default
        _method = self._method

        self._se = np.sqrt(np.diagonal(_vcov))
        self._tstat = _beta_hat / self._se

        df = _N - _k if _vcov_type in ["iid", "hetero"] else _G - 1

        # use t-dist for linear models, but normal for non-linear models
        if _method == "feols":
            self._pvalue = 2 * (1 - t.cdf(np.abs(self._tstat), df))
            z = np.abs(t.ppf(alpha / 2, df))
        else:
            self._pvalue = 2 * (1 - norm.cdf(np.abs(self._tstat)))
            z = np.abs(norm.ppf(alpha / 2))

        z_se = z * self._se
        self._conf_int = np.array([_beta_hat - z_se, _beta_hat + z_se])

    def add_fixest_multi_context(
        self,
        fml: str,
        depvar: str,
        Y: pd.Series,
        _data: pd.DataFrame,
        _ssc_dict: dict[str, Union[str, bool]],
        _k_fe: int,
        fval: str,
        store_data: bool,
    ) -> None:
        """
        Enrich Feols object.

        Enrich an instance of `Feols` Class with additional
        attributes set in the `FixestMulti` class.

        Parameters
        ----------
        fml : str
            The formula used for estimation.
        depvar : str
            The dependent variable of the regression model.
        Y : pd.Series
            The dependent variable of the regression model.
        _data : pd.DataFrame
            The data used for estimation.
        _ssc_dict : dict
            A dictionary with the sum of squares and cross products matrices.
        _k_fe : int
            The number of fixed effects.
        fval : str
            The fixed effects formula.
        store_data : bool
            Indicates whether to save the data used for estimation in the object

        Returns
        -------
        None
        """
        # some bookkeeping
        self._fml = fml
        self._depvar = depvar
        self._Y_untransformed = Y
        self._data = pd.DataFrame()

        if store_data:
            self._data = _data

        self._ssc_dict = _ssc_dict
        self._k_fe = _k_fe
        if fval != "0":
            self._has_fixef = True
            self._fixef = fval
        else:
            self._has_fixef = False

    def _clear_attributes(self):
        attributes = [
            "_X",
            "_Y",
            "_Z",
            "_data",
            "_cluster_df",
            "_tXZ",
            "_tZy",
            "_tZX",
            "_weights",
            "_scores",
            "_tZZinv",
            "_u_hat",
            "_Y_hat_link",
            "_Y_hat_response",
            "_Y_untransformed",
        ]

        for attr in attributes:
            if hasattr(self, attr):
                delattr(self, attr)
        gc.collect()

    def wald_test(self, R=None, q=None, distribution="F"):
        """
        Conduct Wald test.

        Compute a Wald test for a linear hypothesis of the form R * β = q.
        where R is m x k matrix, β is a k x 1 vector of coefficients,
        and q is m x 1 vector.
        By default, tests the joint null hypothesis that all coefficients are zero.

        This method producues the following attriutes

        _dfd : int
            degree of freedom in denominator
        _dfn : int
            degree of freedom in numerator
        _wald_statistic : scalar
            Wald-statistics computed for hypothesis testing
        _f_statistic : scalar
            Wald-statistics(when R is an indentity matrix, and q being zero vector)
            computed for hypothesis testing
        _p_value : scalar
            corresponding p-value for statistics

        Parameters
        ----------
        R : array-like, optional
            The matrix R of the linear hypothesis.
            If None, defaults to an identity matrix.
        q : array-like, optional
            The vector q of the linear hypothesis.
            If None, defaults to a vector of zeros.
        distribution : str, optional
            The distribution to use for the p-value. Can be either "F" or "chi2".
            Defaults to "F".

        Returns
        -------
        pd.Series
            A pd.Series with the Wald statistic and p-value.

        Examples
        --------
        import numpy as np
        import pandas as pd

        from pyfixest.estimation.estimation import feols

        data = pd.read_csv("pyfixest/did/data/df_het.csv")
        data = data.iloc[1:3000]

        R = np.array([[1,-1]] )
        q = np.array([0.0])

        fml = "dep_var ~ treat"
        fit = feols(fml, data, vcov={"CRV1": "year"}, ssc=ssc(adj=False))

        # Wald test
        fit.wald_test(R=R, q=q, distribution = "chi2")
        f_stat = fit._f_statistic
        p_stat = fit._p_value

        print(f"Python f_stat: {f_stat}")
        print(f"Python p_stat: {p_stat}")

        # The code above produces the following results :
        # Python f_stat: 256.55432910297003
        # Python p_stat: 9.67406627744023e-58
        """
        _vcov = self._vcov
        _N = self._N
        _k = self._k
        _beta_hat = self._beta_hat
        _k_fe = np.sum(self._k_fe.values) if self._has_fixef else 0

        # If R is None, default to the identity matrix
        if R is None:
            R = np.eye(_k)

        # Ensure R is two-dimensional
        if R.ndim == 1:
            R = R.reshape((1, len(R)))

        if R.shape[1] != _k:
            raise ValueError(
                "The number of columns of R must be equal to the number of coefficients."
            )

        # If q is None, default to a vector of zeros
        if q is None:
            q = np.zeros(R.shape[0])
        else:
            if not isinstance(q, (int, float, np.ndarray)):
                raise ValueError("q must be a numeric scalar or array.")
            if isinstance(q, np.ndarray):
                if q.ndim != 1:
                    raise ValueError("q must be a one-dimensional array or a scalar.")
                if q.shape[0] != R.shape[0]:
                    raise ValueError("q must have the same number of rows as R.")

        n_restriction = R.shape[0]
        self._dfn = n_restriction

        if self._is_clustered:
            self._dfd = np.min(np.array(self._G)) - 1
        else:
            self._dfd = _N - _k - _k_fe

        bread = R @ _beta_hat - q
        meat = np.linalg.inv(R @ _vcov @ R.T)
        W = bread.T @ meat @ bread
        self._wald_statistic = W

        # Check if distribution is "F" and R is not identity matrix
        # or q is not zero vector
        if distribution == "F" and (
            not np.array_equal(R, np.eye(_k)) or not np.all(q == 0)
        ):
            warnings.warn(
                "Distribution changed to chi2, as R is not an identity matrix and q is not a zero vector."
            )
            distribution = "chi2"

        if distribution == "F":
            self._f_statistic = W / self._dfn
            self._p_value = 1 - f.cdf(self._f_statistic, dfn=self._dfn, dfd=self._dfd)
            res = pd.Series({"statistic": self._f_statistic, "pvalue": self._p_value})
        elif distribution == "chi2":
            self._f_statistic = W / self._dfn
            self._p_value = chi2.sf(self._wald_statistic, self._dfn)
            res = pd.Series(
                {"statistic": self._wald_statistic, "pvalue": self._p_value}
            )
        else:
            raise ValueError("Distribution must be F or chi2")

        return res

    def wildboottest(
        self,
        reps: int,
        cluster: Optional[str] = None,
        param: Optional[str] = None,
        weights_type: Optional[str] = "rademacher",
        impose_null: Optional[bool] = True,
        bootstrap_type: Optional[str] = "11",
        seed: Optional[int] = None,
        adj: Optional[bool] = True,
        cluster_adj: Optional[bool] = True,
        parallel: Optional[bool] = False,
        return_bootstrapped_t_stats=False,
    ):
        """
        Run a wild cluster bootstrap based on an object of type "Feols".

        Parameters
        ----------
        reps : int
            The number of bootstrap iterations to run.
        cluster : Union[str, None], optional
            The variable used for clustering. Defaults to None. If None, then
            uses the variable specified in the model's `clustervar` attribute.
            If no `_clustervar` attribute is found, runs a heteroskedasticity-
            robust bootstrap.
        param : Union[str, None], optional
            A string of length one, containing the test parameter of interest.
            Defaults to None.
        weights_type : str, optional
            The type of bootstrap weights. Options are 'rademacher', 'mammen',
            'webb', or 'normal'. Defaults to 'rademacher'.
        impose_null : bool, optional
            Indicates whether to impose the null hypothesis on the bootstrap DGP.
            Defaults to True.
        bootstrap_type : str, optional
            A string of length one to choose the bootstrap type.
            Options are '11', '31', '13', or '33'. Defaults to '11'.
        seed : Union[int, None], optional
            An option to provide a random seed. Defaults to None.
        adj : bool, optional
            Indicates whether to apply a small sample adjustment for the number
            of observations and covariates. Defaults to True.
        cluster_adj : bool, optional
            Indicates whether to apply a small sample adjustment for the number
            of clusters. Defaults to True.
        parallel : bool, optional
            Indicates whether to run the bootstrap in parallel. Defaults to False.
        seed : Union[str, None], optional
            An option to provide a random seed. Defaults to None.
        return_bootstrapped_t_stats : bool, optional:
            If True, the method returns a tuple of the regular output and the
            bootstrapped t-stats. Defaults to False.

        Returns
        -------
        pd.DataFrame
            A DataFrame with the original, non-bootstrapped t-statistic and
            bootstrapped p-value, along with the bootstrap type, inference type
            (HC vs CRV), and whether the null hypothesis was imposed on the
            bootstrap DGP. If `return_bootstrapped_t_stats` is True, the method
            returns a tuple of the regular output and the bootstrapped t-stats.
        """
        _is_iv = self._is_iv
        _has_fixef = self._has_fixef
        _Y = self._Y.flatten()
        _X = self._X
        _xnames = self._coefnames
        _data = self._data
        _clustervar = self._clustervar
        _supports_wildboottest = self._supports_wildboottest

        if param is not None and param not in _xnames:
            raise ValueError(
                f"Parameter {param} not found in the model's coefficients."
            )

        if not _supports_wildboottest:
            if self._is_iv:
                raise NotImplementedError(
                    "Wild cluster bootstrap is not supported for IV estimation."
                )
            if self._has_weights:
                raise NotImplementedError(
                    "Wild cluster bootstrap is not supported for WLS estimation."
                )

        cluster_list = []

        if cluster is not None and isinstance(cluster, str):
            cluster_list = [cluster]
        if cluster is not None and isinstance(cluster, list):
            cluster_list = cluster

        if cluster is None and _clustervar is not None:
            if isinstance(_clustervar, str):
                cluster_list = [_clustervar]
            else:
                cluster_list = _clustervar

        run_heteroskedastic = not cluster_list

        if not run_heteroskedastic and not len(cluster_list) == 1:
            raise NotImplementedError(
                "Multiway clustering is currently not supported with the wild cluster bootstrap."
            )

        if not run_heteroskedastic and cluster_list[0] not in _data.columns:
            raise ValueError(
                f"Cluster variable {cluster_list[0]} not found in the data."
            )

        try:
            from wildboottest.wildboottest import WildboottestCL, WildboottestHC
        except ImportError:
            print(
                "Module 'wildboottest' not found. Please install 'wildboottest', e.g. via `PyPi`."
            )

        if _is_iv:
            raise NotImplementedError(
                "Wild cluster bootstrap is not supported with IV estimation."
            )

        if self._method == "fepois":
            raise NotImplementedError(
                "Wild cluster bootstrap is not supported for Poisson regression."
            )

        if _has_fixef:
            # update _X, _xnames
            fml_linear, fixef = self._fml.split("|")
            fixef_vars = fixef.split("+")
            # wrap all fixef vars in "C()"
            fixef_vars_C = [f"C({x})" for x in fixef_vars]
            fixef_fml = "+".join(fixef_vars_C)

            fml_dummies = f"{fml_linear} + {fixef_fml}"

            # make this sparse once wildboottest allows it
            _, _X = Formula(fml_dummies).get_model_matrix(_data, output="numpy")
            _xnames = _X.model_spec.column_names

        # later: allow r <> 0 and custom R
        R = np.zeros(len(_xnames))
        if param is not None:
            R[_xnames.index(param)] = 1
        r = 0

        if run_heteroskedastic:
            inference = "HC"

            boot = WildboottestHC(X=_X, Y=_Y, R=R, r=r, B=reps, seed=seed)
            boot.get_adjustments(bootstrap_type=bootstrap_type)
            boot.get_uhat(impose_null=impose_null)
            boot.get_tboot(weights_type=weights_type)
            boot.get_tstat()
            boot.get_pvalue(pval_type="two-tailed")
            full_enumeration_warn = False

        else:
            inference = f"CRV({cluster_list[0]})"

            cluster_array = _data[cluster_list[0]].to_numpy().flatten()

            boot = WildboottestCL(
                X=_X,
                Y=_Y,
                cluster=cluster_array,
                R=R,
                B=reps,
                seed=seed,
                parallel=parallel,
            )
            boot.get_scores(
                bootstrap_type=bootstrap_type,
                impose_null=impose_null,
                adj=adj,
                cluster_adj=cluster_adj,
            )
            _, _, full_enumeration_warn = boot.get_weights(weights_type=weights_type)
            boot.get_numer()
            boot.get_denom()
            boot.get_tboot()
            boot.get_vcov()
            boot.get_tstat()
            boot.get_pvalue(pval_type="two-tailed")

            if full_enumeration_warn:
                warnings.warn(
                    "2^G < the number of boot iterations, setting full_enumeration to True."
                )

        if np.isscalar(boot.t_stat):
            boot.t_stat = np.asarray(boot.t_stat)
        else:
            boot.t_stat = boot.t_stat[0]

        res = {
            "param": param,
            "t value": boot.t_stat.astype(np.float64),
            "Pr(>|t|)": np.asarray(boot.pvalue).astype(np.float64),
            "bootstrap_type": bootstrap_type,
            "inference": inference,
            "impose_null": impose_null,
        }

        res_df = pd.Series(res)

        if return_bootstrapped_t_stats:
            return res_df, boot.t_boot
        else:
            return res_df

    def ccv(
        self,
        treatment,
        cluster: Optional[str] = None,
        seed: Optional[int] = None,
        n_splits: int = 8,
        pk: float = 1,
        qk: float = 1,
    ) -> pd.DataFrame:
        """
        Compute the Causal Cluster Variance following Abadie et al (QJE 2023).

        Parameters
        ----------
        treatment: str
            The name of the treatment variable.
        cluster : str
            The name of the cluster variable. None by default.
            If None, uses the cluster variable from the model fit.
        seed : int, optional
            An integer to set the random seed. Defaults to None.
        n_splits : int, optional
            The number of splits to use in the cross-fitting procedure. Defaults to 8.
        pk: float, optional
            The proportion of sampled clusters. Defaults to 1, which
            corresponds to all clusters of the population being sampled.
        qk: float, optional
            The proportion of sampled observations within each cluster.
            Defaults to 1, which corresponds to all observations within
            each cluster being sampled.

        Returns
        -------
        pd.DataFrame
            A DataFrame with inference based on the "Causal Cluster Variance"
            and "regular" CRV1 inference.

        Examples
        --------
        ```python
        from pyfixest.estimation import feols
        from pyfixest.utils import get_data

        data = get_data()
        data["D1"] = np.random.choice([0, 1], size=data.shape[0])

        fit = feols("Y ~ D", data=data, vcov={"CRV1": "group_id"})
        fit.ccv(treatment="D", pk=0.05, gk=0.5, n_splits=8, seed=123).head()
        ```
        """
        assert (
            self._supports_cluster_causal_variance
        ), "The model does not support the causal cluster variance estimator."
        assert isinstance(treatment, str), "treatment must be a string."
        assert (
            isinstance(cluster, str) or cluster is None
        ), "cluster must be a string or None."
        assert isinstance(seed, int) or seed is None, "seed must be an integer or None."
        assert isinstance(n_splits, int), "n_splits must be an integer."
        assert isinstance(pk, (int, float)) and 0 <= pk <= 1
        assert isinstance(qk, (int, float)) and 0 <= qk <= 1

        if self._has_fixef:
            raise NotImplementedError(
                "The causal cluster variance estimator is currently not supported for models with fixed effects."
            )

        if treatment not in self._coefnames:
            raise ValueError(
                f"Variable {treatment} not found in the model's coefficients."
            )

        if cluster is None:
            if self._clustervar is None:
                raise ValueError("No cluster variable found in the model fit.")
            elif len(self._clustervar) > 1:
                raise ValueError(
                    "Multiway clustering is currently not supported with the causal cluster variance estimator."
                )
            else:
                cluster = self._clustervar[0]

        # check that cluster is in data
        if cluster not in self._data.columns:
            raise ValueError(
                f"Cluster variable {cluster} not found in the data used for the model fit."
            )

        if not self._is_clustered:
            warnings.warn(
                "The initial model was not clustered. CRV1 inference is computed and stored in the model object."
            )
            self.vcov({"CRV1": cluster})

        if seed is None:
            seed = np.random.randint(1, 100_000_000)
        rng = np.random.default_rng(seed)

        depvar = self._depvar
        fml = self._fml
        xfml_list = fml.split("~")[1].split("+")
        xfml_list = [x for x in xfml_list if x != treatment]
        xfml = "" if not xfml_list else "+".join(xfml_list)

        data = self._data
        Y = self._Y.flatten()
        W = data[treatment].to_numpy()
        assert np.all(
            np.isin(W, [0, 1])
        ), "Treatment variable must be binary with values 0 and 1"
        X = self._X
        cluster_vec = data[cluster].to_numpy()
        unique_clusters = np.unique(cluster_vec)

        tau_full = np.array(self.coef().xs(treatment))

        N = self._N
        G = len(unique_clusters)

        ccv_module = import_module("pyfixest.estimation.ccv")
        _compute_CCV = getattr(ccv_module, "_compute_CCV")

        vcov_splits = 0.0
        for _ in range(n_splits):
            vcov_ccv = _compute_CCV(
                fml=fml,
                Y=Y,
                X=X,
                W=W,
                rng=rng,
                data=data,
                treatment=treatment,
                cluster_vec=cluster_vec,
                pk=pk,
                tau_full=tau_full,
            )
            vcov_splits += vcov_ccv

        vcov_splits /= n_splits
        vcov_splits /= N

        crv1_idx = self._coefnames.index(treatment)
        vcov_crv1 = self._vcov[crv1_idx, crv1_idx]
        vcov_ccv = qk * vcov_splits + (1 - qk) * vcov_crv1

        se = np.sqrt(vcov_ccv)
        tstat = tau_full / se
        df = G - 1
        pvalue = 2 * (1 - t.cdf(np.abs(tstat), df))
        alpha = 0.95
        z = np.abs(t.ppf((1 - alpha) / 2, df))
        z_se = z * se
        conf_int = np.array([tau_full - z_se, tau_full + z_se])

        res_ccv_dict: dict[str, Union[float, np.ndarray]] = {
            "Estimate": tau_full,
            "Std. Error": se,
            "t value": tstat,
            "Pr(>|t|)": pvalue,
            "2.5%": conf_int[0],
            "97.5%": conf_int[1],
        }

        res_ccv = pd.Series(res_ccv_dict)

        res_ccv.name = "CCV"

        res_crv1 = self.tidy().xs(treatment)
        res_crv1.name = "CRV1"

        return pd.concat([res_ccv, res_crv1], axis=1).T

        ccv_module = import_module("pyfixest.estimation.ccv")
        _ccv = getattr(ccv_module, "_ccv")

        return _ccv(
            data=data,
            depvar=depvar,
            treatment=treatment,
            cluster=cluster,
            xfml=xfml,
            seed=seed,
            pk=pk,
            qk=qk,
            n_splits=n_splits,
        )

    def fixef(
        self, atol: float = 1e-06, btol: float = 1e-06
    ) -> dict[str, dict[str, float]]:
        """
        Compute the coefficients of (swept out) fixed effects for a regression model.

        This method creates the following attributes:
        - `alphaDF` (pd.DataFrame): A DataFrame with the estimated fixed effects.
        - `sumFE` (np.array): An array with the sum of fixed effects for each
        observation (i = 1, ..., N).

        Returns
        -------
        None
        """
        _has_fixef = self._has_fixef
        _is_iv = self._is_iv
        _method = self._method
        _fml = self._fml
        _data = self._data

        if not _has_fixef:
            raise ValueError("The regression model does not have fixed effects.")

        if _is_iv:
            raise NotImplementedError(
                "The fixef() method is currently not supported for IV models."
            )

        # fixef_vars = self._fixef.split("+")[0]

        depvars, rhs = _fml.split("~")
        covars, fixef_vars = rhs.split("|")

        fixef_vars_list = fixef_vars.split("+")
        fixef_vars_C = [f"C({x})" for x in fixef_vars_list]
        fixef_fml = "+".join(fixef_vars_C)

        fml_linear = f"{depvars} ~ {covars}"
        Y, X = Formula(fml_linear).get_model_matrix(_data, output="pandas")
        if self._X_is_empty:
            Y = Y.to_numpy()
            uhat = Y

        else:
            X = X[self._coefnames]  # drop intercept, potentially multicollinear vars
            Y = Y.to_numpy().flatten().astype(np.float64)
            X = X.to_numpy()
            uhat = (Y - X @ self._beta_hat).flatten()

        D2 = Formula("-1+" + fixef_fml).get_model_matrix(_data, output="sparse")
        cols = D2.model_spec.column_names

        alpha = lsqr(D2, uhat, atol=atol, btol=btol)[0]

        res: dict[str, dict[str, float]] = {}
        for i, col in enumerate(cols):
            variable, level = _extract_variable_level(col)
            # check if res already has a key variable
            if variable not in res:
                res[variable] = dict()
                res[variable][level] = alpha[i]
                continue
            else:
                if level not in res[variable]:
                    res[variable][level] = alpha[i]

        self._fixef_dict = res
        self._alpha = alpha
        self._sumFE = D2.dot(alpha)

        return self._fixef_dict

    def predict(
        self,
        newdata: Optional[DataFrameType] = None,
        atol: float = 1e-6,
        btol: float = 1e-6,
        type: str = "link",
        compute_stdp: bool = False
    ) -> pd.DataFrame:
        """
        Predict values of the model on new data.

        Return a flat np.array with predicted values of the regression model.
        If new fixed effect levels are introduced in `newdata`, predicted values
        for such observations will be set to NaN.

        Parameters
        ----------
        newdata : Optional[DataFrameType], optional
            A pd.DataFrame or pl.DataFrame with the data to be used for prediction.
            If None (default), the data used for fitting the model is used.
        type : str, optional
            The type of prediction to be computed.
            Can be either "response" (default) or "link". For linear models, both are
            identical.
        atol : Float, default 1e-6
            Stopping tolerance for scipy.sparse.linalg.lsqr().
            See https://docs.scipy.org/doc/
                scipy/reference/generated/scipy.sparse.linalg.lsqr.html
        btol : Float, default 1e-6
            Another stopping tolerance for scipy.sparse.linalg.lsqr().
            See https://docs.scipy.org/doc/
                scipy/reference/generated/scipy.sparse.linalg.lsqr.html
        type:
            The type of prediction to be made. Can be either 'link' or 'response'.
             Defaults to 'link'. 'link' and 'response' lead
            to identical results for linear models.
        compute_stdp: boolean
            Whether to compute standard error of the predictions

        Returns
        -------
        pred_results : pd.DataFrame
            Dataframe with columns "y_hat" and optionally "stdp" (if compute_stdp is True)
        """
        if self._is_iv:
            raise NotImplementedError(
                "The predict() method is currently not supported for IV models."
            )

        prediction_df = pd.DataFrame()
        if newdata is None:
            prediction_df["yhat"] = self._Y_untransformed.to_numpy().flatten() - self._u_hat.flatten()
            if compute_stdp:
                prediction_df["stdp"] = self.get_newdata_stdp(self._X)
            return prediction_df

        newdata = _polars_to_pandas(newdata).reset_index(drop=False)

        if not self._X_is_empty:
            xfml = self._fml.split("|")[0].split("~")[1]
            X = Formula(xfml).get_model_matrix(newdata)
            X_index = X.index
            coef_idx = np.isin(self._coefnames, X.columns)
            X = X[np.array(self._coefnames)[coef_idx]]
            X = X.to_numpy()
            y_hat = np.full(newdata.shape[0], np.nan)
            y_hat[X_index] = X @ self._beta_hat[coef_idx]
        else:
            y_hat = np.zeros(newdata.shape[0])

        if self._has_fixef:
            if self._sumFE is None:
                self.fixef(atol, btol)
            fvals = self._fixef.split("+")
            df_fe = newdata[fvals].astype(str)
            # populate fixed effect dicts with omitted categories handling
            fixef_dicts = {}
            for f in fvals:
                fdict = self._fixef_dict[f"C({f})"]
                omitted_cat = set(self._data[f].unique().astype(str).tolist()) - set(
                    fdict.keys()
                )
                if omitted_cat:
                    fdict.update({x: 0 for x in omitted_cat})
                fixef_dicts[f"C({f})"] = fdict
            _fixef_mat = _apply_fixef_numpy(df_fe.values, fixef_dicts)
            y_hat += np.sum(_fixef_mat, axis=1)

        prediction_df["yhat"] = y_hat.flatten()
        if compute_stdp:
            stdp_df = np.full(newdata.shape[0], np.nan)
            stdp_df[X_index] = self.get_newdata_stdp(X)
            prediction_df["stdp"] = stdp_df

        return prediction_df


    def get_newdata_stdp(self, X):
        """
        Get standard error of predictions for new data.

        Parameters
        ----------
        X : np.ndarray
            Covariates for new data points.

        Returns
        -------
        list
            Standard errors for each prediction
        """
        # for now only compute prediction error if model has no fixed effects
        # TODO: implement for fixed effects
        if not self._has_fixef:
            if not self._X_is_empty:
                return list(map(self.get_single_row_stdp, X))
            else:
                warnings.warn(
                    """
                    Standard error of the prediction cannot be computed if X is empty.
                    Prediction dataframe stdp column will be None.
                    """
                )
        else:
            warnings.warn(
                """
                Standard error of the prediction is not implemented for fixed effects models.
                Prediction dataframe stdp column will be None.
                """
            )

    def get_single_row_stdp(self, row):
        """
        Get standard error of predictions for a single row.

        Parameters
        ----------
        row : np.ndarray
            Single row of new covariate data

        Returns
        -------
        np.ndarray
            Standard error of prediction for single row
        """
        return np.linalg.multi_dot([row, self._vcov, np.transpose(row)])

    def get_nobs(self):
        """
        Fetch the number of observations used in fitting the regression model.

        Returns
        -------
        None
        """
        self._N_rows = len(self._Y)
        if self._weights_type == "aweights":
            self._N = self._N_rows
        elif self._weights_type == "fweights":
            self._N = np.sum(self._weights)

    def get_performance(self) -> None:
        """
        Get Goodness-of-Fit measures.

        Compute multiple additional measures commonly reported with linear
        regression output, including R-squared and adjusted R-squared. Note that
        variables with the suffix _within use demeaned dependent variables Y,
        while variables without do not or are invariant to demeaning.

        Returns
        -------
        None

        Creates the following instances:
        - r2 (float): R-squared of the regression model.
        - adj_r2 (float): Adjusted R-squared of the regression model.
        - r2_within (float): R-squared of the regression model, computed on
        demeaned dependent variable.
        - adj_r2_within (float): Adjusted R-squared of the regression model,
        computed on demeaned dependent variable.
        """
        _Y_within = self._Y
        _Y = self._Y_untransformed.to_numpy()

        _u_hat = self._u_hat
        _N = self._N
        _k = self._k
        _has_fixef = self._has_fixef
        _weights = self._weights
        _has_weights = self._has_weights

        if _has_fixef:
            _k_fe = np.sum(self._k_fe - 1) + 1
            _adj_factor = (_N - _k_fe) / (_N - _k - _k_fe)
        else:
            _adj_factor = (_N) / (_N - 1)

        ssu = np.sum(_u_hat**2)

        ssy = np.sum((_Y - np.mean(_Y)) ** 2)

        if _has_weights:
            self._rmse = np.nan
            self._r2 = np.nan
            self._adj_r2 = np.nan
        else:
            self._rmse = np.sqrt(ssu / _N)
            self._r2 = 1 - (ssu / ssy)
            self._adj_r2 = 1 - (ssu / ssy) * _adj_factor

        if _has_fixef and not _has_weights:
            ssy_within = np.sum((_Y_within - np.mean(_Y_within)) ** 2)
            self._r2_within = 1 - (ssu / ssy_within)
            self._r2_adj_within = 1 - (ssu / ssy_within) * _adj_factor
        else:
            self._r2_within = np.nan
            self._adj_r2_within = np.nan

        # overwrite self._adj_r2 and self._adj_r2_within
        # reason: currently I cannot match fixest dof correction, so
        # better not to report it
        self._adj_r2 = np.nan
        self._adj_r2_within = np.nan

    def tidy(
        self,
        alpha: Optional[float] = None,
    ) -> pd.DataFrame:
        """
        Tidy model outputs.

        Return a tidy pd.DataFrame with the point estimates, standard errors,
        t-statistics, and p-values.

        Parameters
        ----------
        alpha: Optional[float]
            The significance level for the confidence intervals. If None,
            computes a 95% confidence interval (`alpha = 0.05`).

        Returns
        -------
        tidy_df : pd.DataFrame
            A tidy pd.DataFrame containing the regression results, including point
            estimates, standard errors, t-statistics, and p-values.
        """
        if alpha is None:
            ub, lb = 0.975, 0.025
            self.get_inference()
        else:
            ub, lb = 1 - alpha / 2, alpha / 2
            self.get_inference(alpha=alpha)

        _coefnames = self._coefnames
        _se = self._se
        _tstat = self._tstat
        _pvalue = self._pvalue
        _beta_hat = self._beta_hat
        _conf_int = self._conf_int

        tidy_df = pd.DataFrame(
            {
                "Coefficient": _coefnames,
                "Estimate": _beta_hat,
                "Std. Error": _se,
                "t value": _tstat,
                "Pr(>|t|)": _pvalue,
                f"{lb*100:.1f}%": _conf_int[0],
                f"{ub*100:.1f}%": _conf_int[1],
            }
        )

        return tidy_df.set_index("Coefficient")

    def coef(self) -> pd.Series:
        """
        Fitted model coefficents.

        Returns
        -------
        pd.Series
            A pd.Series with the estimated coefficients of the regression model.
        """
        return self.tidy()["Estimate"]

    def se(self) -> pd.Series:
        """
        Fitted model standard errors.

        Returns
        -------
        pd.Series
            A pd.Series with the standard errors of the estimated regression model.
        """
        return self.tidy()["Std. Error"]

    def tstat(self) -> pd.Series:
        """
        Fitted model t-statistics.

        Returns
        -------
        pd.Series
            A pd.Series with t-statistics of the estimated regression model.
        """
        return self.tidy()["t value"]

    def pvalue(self) -> pd.Series:
        """
        Fitted model p-values.

        Returns
        -------
        pd.Series
            A pd.Series with p-values of the estimated regression model.
        """
        return self.tidy()["Pr(>|t|)"]

    def confint(
        self,
        alpha: float = 0.05,
        keep: Optional[Union[list, str]] = None,
        drop: Optional[Union[list, str]] = None,
        exact_match: Optional[bool] = False,
        joint: bool = False,
        seed: Optional[int] = None,
        reps: int = 10_000,
    ) -> pd.DataFrame:
        r"""
        Fitted model confidence intervals.

        Parameters
        ----------
        alpha : float, optional
            The significance level for confidence intervals. Defaults to 0.05.
            keep: str or list of str, optional
        joint : bool, optional
            Whether to compute simultaneous confidence interval for joint null
            of parameters selected by `keep` and `drop`. Defaults to False. See
            https://www.causalml-book.org/assets/chapters/CausalML_chap_4.pdf,
            Remark 4.4.1 for details.
        keep: str or list of str, optional
            The pattern for retaining coefficient names. You can pass a string (one
            pattern) or a list (multiple patterns). Default is keeping all coefficients.
            You should use regular expressions to select coefficients.
                "age",            # would keep all coefficients containing age
                r"^tr",           # would keep all coefficients starting with tr
                r"\\d$",          # would keep all coefficients ending with number
            Output will be in the order of the patterns.
        drop: str or list of str, optional
            The pattern for excluding coefficient names. You can pass a string (one
            pattern) or a list (multiple patterns). Syntax is the same as for `keep`.
            Default is keeping all coefficients. Parameter `keep` and `drop` can be
            used simultaneously.
        exact_match: bool, optional
            Whether to use exact match for `keep` and `drop`. Default is False.
            If True, the pattern will be matched exactly to the coefficient name
            instead of using regular expressions.
        reps : int, optional
            The number of bootstrap iterations to run for joint confidence intervals.
            Defaults to 10_000. Only used if `joint` is True.
        seed : int, optional
            The seed for the random number generator. Defaults to None. Only used if
            `joint` is True.

        Returns
        -------
        pd.DataFrame
            A pd.DataFrame with confidence intervals of the estimated regression model
            for the selected coefficients.

        Examples
        --------
        ```python
        from pyfixest.utils import get_data
        from pyfixest.estimation import feols

        data = get_data()
        fit = feols("Y ~ C(f1)", data=data)
        fit.confint(alpha=0.10).head()
        fit.confint(alpha=0.10, joint=True, reps=9999).head()
        ```
        """
        if keep is None:
            keep = []
        if drop is None:
            drop = []

        tidy_df = self.tidy()
        if keep or drop:
            if isinstance(keep, str):
                keep = [keep]
            if isinstance(drop, str):
                drop = [drop]
            idxs = _select_order_coefs(tidy_df.index.tolist(), keep, drop, exact_match)
            coefnames = tidy_df.loc[idxs, :].index.tolist()
        else:
            coefnames = self._coefnames

        joint_indices = [i for i, x in enumerate(self._coefnames) if x in coefnames]
        if not joint_indices:
            raise ValueError("No coefficients match the keep/drop patterns.")

        if not joint:
            if self._vcov_type in ["iid", "hetero"]:
                df = self._N - self._k
            else:
                _G = np.min(np.array(self._G))  # fixest default
                df = _G - 1

            # use t-dist for linear models, but normal for non-linear models
            if self._method == "feols":
                crit_val = np.abs(t.ppf(alpha / 2, df))
            else:
                crit_val = np.abs(norm.ppf(alpha / 2))
        else:
            D_inv = 1 / self._se[joint_indices]
            V = self._vcov[np.ix_(joint_indices, joint_indices)]
            C_coefs = (D_inv * V).T * D_inv
            crit_val = simultaneous_crit_val(C_coefs, reps, alpha=alpha, seed=seed)

        ub = pd.Series(
            self._beta_hat[joint_indices] + crit_val * self._se[joint_indices]
        )
        lb = pd.Series(
            self._beta_hat[joint_indices] - crit_val * self._se[joint_indices]
        )

        df = pd.DataFrame(
            {
                f"{alpha / 2*100:.1f}%": lb,
                f"{(1-alpha / 2)*100:.1f}%": ub,
            }
        )
        # df = pd.DataFrame({f"{alpha / 2}%": lb, f"{1-alpha / 2}%": ub})
        df.index = coefnames

        return df

    def resid(self) -> np.ndarray:
        """
        Fitted model residuals.

        Returns
        -------
        np.ndarray
            A np.ndarray with the residuals of the estimated regression model.
        """
        return self._u_hat

    def ritest(
        self,
        resampvar: str,
        cluster: Optional[str] = None,
        reps: int = 100,
        type: str = "randomization-c",
        rng: Optional[np.random.Generator] = None,
        choose_algorithm: str = "auto",
        store_ritest_statistics: bool = False,
        level: float = 0.95,
    ) -> pd.Series:
        """
        Conduct Randomization Inference (RI) test against a null hypothesis of
        `resampvar = 0`.

        Parameters
        ----------
        resampvar : str
            The name of the variable to be resampled.
        cluster : str, optional
            The name of the cluster variable in case of cluster random assignment.
            If provided, `resampvar` is held constant within each `cluster`.
            Defaults to None.
        reps : int, optional
            The number of randomization iterations. Defaults to 100.
        type: str
            The type of the randomization inference test.
            Can be "randomization-c" or "randomization-t". Note that
            the "randomization-c" is much faster, while the
            "randomization-t" is recommended by Wu & Ding (JASA, 2021).
        rng : np.random.Generator, optional
            A random number generator. Defaults to None.
        choose_algorithm: str, optional
            The algorithm to use for the computation. Defaults to "auto".
            The alternative is "fast" and "slow", and should only be used
            for running CI tests. Ironically, this argument is not tested
            for any input errors from the user! So please don't use it =)
        include_plot: bool, optional
            Whether to include a plot of the distribution p-values. Defaults to False.
        store_ritest_statistics: bool, optional
            Whether to store the simulated statistics of the RI procedure.
            Defaults to False. If True, stores the simulated statistics
            in the model object via the `ritest_statistics` attribute as a
            numpy array.
        level: float, optional
            The level for the confidence interval of the randomization inference
            p-value. Defaults to 0.95.

        Returns
        -------
        A pd.Series with the regression coefficient of `resampvar` and the p-value
        of the RI test. Additionally, reports the standard error and the confidence
        interval of the p-value.
        """
        _fml = self._fml
        _data = self._data
        _method = self._method
        _is_iv = self._is_iv
        _coefnames = self._coefnames
        _has_fixef = self._has_fixef

        resampvar = resampvar.replace(" ", "")
        resampvar_, h0_value, hypothesis, test_type = _decode_resampvar(resampvar)

        if _is_iv:
            raise NotImplementedError(
                "Randomization Inference is not supported for IV models."
            )

        # check that resampvar in _coefnames
        if resampvar_ not in _coefnames:
            raise ValueError(f"{resampvar_} not found in the model's coefficients.")

        if cluster is not None and cluster not in _data:
            raise ValueError(f"The variable {cluster} is not found in the data.")

        sample_coef = np.array(self.coef().xs(resampvar_))
        sample_tstat = np.array(self.tstat().xs(resampvar_))

        clustervar_arr = _data[cluster].to_numpy().reshape(-1, 1) if cluster else None

        rng = np.random.default_rng() if rng is None else rng

        sample_stat = sample_tstat if type == "randomization-t" else sample_coef

        if clustervar_arr is not None and np.any(np.isnan(clustervar_arr)):
            raise ValueError(
                """
            The cluster variable contains missing values. This is not allowed
            for randomization inference via `ritest()`.
            """
            )

        if type not in ["randomization-t", "randomization-c"]:
            raise ValueError("type must be 'randomization-t' or 'randomization-c.")

        assert isinstance(reps, int) and reps > 0, "reps must be a positive integer."

        if self._has_weights:
            raise NotImplementedError(
                """
                Regression Weights are not supported with Randomization Inference.
                """
            )

        if choose_algorithm == "slow" or _method == "fepois":
            vcov_input: Union[str, dict[str, str]]
            if cluster is not None:
                vcov_input = {"CRV1": cluster}
            else:
                # "iid" for models without controls, else HC1
                vcov_input = (
                    "hetero"
                    if (_has_fixef and len(_coefnames) > 1) or len(_coefnames) > 2
                    else "iid"
                )

            # for performance reasons
            if type == "randomization-c":
                vcov_input = "iid"

            ri_stats = _get_ritest_stats_slow(
                data=_data,
                resampvar=resampvar_,
                clustervar_arr=clustervar_arr,
                fml=_fml,
                reps=reps,
                vcov=vcov_input,
                type=type,
                rng=rng,
                model=_method,
            )

        else:
            _Y = self._Y
            _X = self._X
            _coefnames = self._coefnames

            _weights = self._weights.flatten()
            _data = self._data
            _fval_df = _data[self._fixef.split("+")] if _has_fixef else None

            _D = self._data[resampvar_].to_numpy()

            ri_stats = _get_ritest_stats_fast(
                Y=_Y,
                X=_X,
                D=_D,
                coefnames=_coefnames,
                resampvar=resampvar_,
                clustervar_arr=clustervar_arr,
                reps=reps,
                rng=rng,
                fval_df=_fval_df,
                weights=_weights,
            )

        ri_pvalue, se_pvalue, ci_pvalue = _get_ritest_pvalue(
            sample_stat=sample_stat,
            ri_stats=ri_stats[1:],
            method=test_type,
            h0_value=h0_value,
            level=level,
        )

        if store_ritest_statistics:
            self._ritest_statistics = ri_stats
            self._ritest_pvalue = ri_pvalue
            self._ritest_sample_stat = sample_stat - h0_value

        res = pd.Series(
            {
                "H0": hypothesis,
                "ri-type": type,
                "Estimate": sample_coef,
                "Pr(>|t|)": ri_pvalue,
                "Std. Error (Pr(>|t|))": se_pvalue,
            }
        )

        alpha = 1 - level
        ci_lower_name = str(f"{alpha/2*100:.1f}% (Pr(>|t|))")
        ci_upper_name = str(f"{(1-alpha/2)*100:.1f}% (Pr(>|t|))")
        res[ci_lower_name] = ci_pvalue[0]
        res[ci_upper_name] = ci_pvalue[1]

        if cluster is not None:
            res["Cluster"] = cluster

        return res

    def plot_ritest(self, plot_backend="lets_plot"):
        """
        Plot the distribution of the Randomization Inference Statistics.

        Parameters
        ----------
        plot_backend : str, optional
            The plotting backend to use. Defaults to "lets_plot". Alternatively,
            "matplotlib" is available.

        Returns
        -------
        A lets_plot or matplotlib figure with the distribution of the Randomization
        Inference Statistics.
        """
        if not hasattr(self, "_ritest_statistics"):
            raise ValueError(
                """
                            The randomization inference statistics have not been stored
                            in the model object. Please set `store_ritest_statistics=True`
                            when calling `ritest()`
                            """
            )

        ri_stats = self._ritest_statistics
        sample_stat = self._ritest_sample_stat

        return _plot_ritest_pvalue(
            ri_stats=ri_stats, sample_stat=sample_stat, plot_backend=plot_backend
        )

    def update(
        self, X_new: np.ndarray, y_new: np.ndarray, inplace: bool = False
    ) -> np.ndarray:
        """
        Update coefficients for new observations using Sherman-Morrison formula.

        Parameters
        ----------
            X : np.ndarray
                Covariates for new data points. Users expected to ensure conformability
                with existing data.
            y : np.ndarray
                Outcome values for new data points
            inplace : bool, optional
                Whether to update the model object in place. Defaults to False.

        Returns
        -------
        np.ndarray
            Updated coefficients
        """
        if self._has_fixef:
            raise NotImplementedError(
                "The update() method is currently not supported for models with fixed effects."
            )
        if not np.all(X_new[:, 0] == 1):
            X_new = np.column_stack((np.ones(len(X_new)), X_new))
        X_n_plus_1 = np.vstack((self._X, X_new))
        epsi_n_plus_1 = y_new - X_new @ self._beta_hat
        gamma_n_plus_1 = np.linalg.inv(X_n_plus_1.T @ X_n_plus_1) @ X_new.T
        beta_n_plus_1 = self._beta_hat + gamma_n_plus_1 @ epsi_n_plus_1
        if inplace:
            self._X = X_n_plus_1
            self._Y = np.append(self._Y, y_new)
            self._beta_hat = beta_n_plus_1
            self._u_hat = self._Y - self._X @ self._beta_hat
            self._N += X_new.shape[0]

        return beta_n_plus_1


def _feols_input_checks(Y: np.ndarray, X: np.ndarray, weights: np.ndarray):
    """
    Perform basic checks on the input matrices Y and X for the FEOLS.

    Parameters
    ----------
    Y : np.ndarray
        FEOLS input matrix Y.
    X : np.ndarray
        FEOLS input matrix X.
    weights : np.ndarray
        FEOLS weights.

    Returns
    -------
    None
    """
    if not isinstance(Y, (np.ndarray)):
        raise TypeError("Y must be a numpy array.")
    if not isinstance(X, (np.ndarray)):
        raise TypeError("X must be a numpy array.")
    if not isinstance(weights, (np.ndarray)):
        raise TypeError("weights must be a numpy array.")

    if Y.ndim != 2:
        raise ValueError("Y must be a 2D array")
    if X.ndim != 2:
        raise ValueError("X must be a 2D array")
    if weights.ndim != 2:
        raise ValueError("weights must be a 2D array")


def _get_vcov_type(vcov: str, fval: str):
    """
    Get variance-covariance matrix type.

    Passes the specified vcov type and sets the default vcov type based on the
    inclusion of fixed effects in the model.

    Parameters
    ----------
    vcov : str
        The specified vcov type.
    fval : str
        The specified fixed effects, formatted as a string (e.g., "X1+X2").

    Returns
    -------
    vcov_type : str
        The specified or default vcov type. Defaults to 'iid' if no fixed effect
        is included in the model, and 'CRV1' clustered by the first fixed effect
        if a fixed effect is included.
    """
    if vcov is None:
        # iid if no fixed effects
        if fval == "0":
            vcov_type = "iid"
        else:
            # CRV1 inference, clustered by first fixed effect
            first_fe = fval.split("+")[0]
            vcov_type = {"CRV1": first_fe}
    else:
        vcov_type = vcov

    return vcov_type


def _drop_multicollinear_variables(
    X: np.ndarray, names: list[str], collin_tol: float
) -> tuple[np.ndarray, list[str], list[str], list[int]]:
    """
    Check for multicollinearity in the design matrices X and Z.

    Parameters
    ----------
    X : numpy.ndarray
        The design matrix X.
    names : list[str]
        The names of the coefficients.
    collin_tol : float
        The tolerance level for the multicollinearity check.

    Returns
    -------
    Xd : numpy.ndarray
        The design matrix X after checking for multicollinearity.
    names : list[str]
        The names of the coefficients, excluding those identified as collinear.
    collin_vars : list[str]
        The collinear variables identified during the check.
    collin_index : numpy.ndarray
        Logical array, where True indicates that the variable is collinear.
    """
    # TODO: avoid doing this computation twice, e.g. compute tXXinv here as fixest does

    tXX = X.T @ X
    id_excl, n_excl, all_removed = _find_collinear_variables(tXX, collin_tol)

    collin_vars = []
    collin_index = []

    if all_removed:
        raise ValueError(
            """
            All variables are collinear. Maybe your model specification introduces multicollinearity? If not, please reach out to the package authors!.
            """
        )

    names_array = np.array(names)
    if n_excl > 0:
        collin_vars = names_array[id_excl].tolist()
        warnings.warn(
            f"""
            The following variables are collinear: {collin_vars}.
            The variables are dropped from the model.
            """
        )

        X = np.delete(X, id_excl, axis=1)
        if X.ndim == 2 and X.shape[1] == 0:
            raise ValueError(
                """
                All variables are collinear. Please check your model specification.
                """
            )

        names_array = np.delete(names_array, id_excl)
        collin_index = id_excl.tolist()

    return X, names_array.tolist(), collin_vars, collin_index


@nb.njit(parallel=False)
def _find_collinear_variables(
    X: np.ndarray, tol: float = 1e-10
) -> tuple[np.ndarray, int, bool]:
    """
    Detect multicollinear variables.

    Detect multicollinear variables, replicating Laurent Berge's C++ implementation
    from the fixest package. See the fixest repo [here](https://github.com/lrberge/fixest/blob/a4d1a9bea20aa7ab7ab0e0f1d2047d8097971ad7/src/lm_related.cpp#L130)

    Parameters
    ----------
    X : numpy.ndarray
        A symmetric matrix X used to check for multicollinearity.
    tol : float
        The tolerance level for the multicollinearity check.

    Returns
    -------
    - id_excl (numpy.ndarray): A boolean array, where True indicates a collinear
        variable.
    - n_excl (int): The number of collinear variables.
    - all_removed (bool): True if all variables are identified as collinear.
    """
    K = X.shape[1]
    R = np.zeros((K, K))
    id_excl = np.zeros(K, dtype=np.int32)
    n_excl = 0
    min_norm = X[0, 0]

    for j in range(K):
        R_jj = X[j, j]
        for k in range(j):
            if id_excl[k]:
                continue
            R_jj -= R[k, j] * R[k, j]

        if R_jj < tol:
            n_excl += 1
            id_excl[j] = 1

            if n_excl == K:
                all_removed = True
                return id_excl.astype(np.bool_), n_excl, all_removed

            continue

        if min_norm > R_jj:
            min_norm = R_jj

        R_jj = np.sqrt(R_jj)
        R[j, j] = R_jj

        for i in range(j + 1, K):
            value = X[i, j]
            for k in range(j):
                if id_excl[k]:
                    continue
                value -= R[k, i] * R[k, j]
            R[j, i] = value / R_jj

    return id_excl.astype(np.bool_), n_excl, False


def _check_vcov_input(vcov: Union[str, dict[str, str]], data: pd.DataFrame):
    """
    Check the input for the vcov argument in the Feols class.

    Parameters
    ----------
    vcov : Union[str, dict[str, str]]
        The vcov argument passed to the Feols class.
    data : pd.DataFrame
        The data passed to the Feols class.

    Returns
    -------
    None
    """
    assert isinstance(vcov, (dict, str, list)), "vcov must be a dict, string or list"
    if isinstance(vcov, dict):
        assert list(vcov.keys())[0] in [
            "CRV1",
            "CRV3",
        ], "vcov dict key must be CRV1 or CRV3"
        assert isinstance(
            list(vcov.values())[0], str
        ), "vcov dict value must be a string"
        deparse_vcov = list(vcov.values())[0].split("+")
        assert len(deparse_vcov) <= 2, "not more than twoway clustering is supported"

    if isinstance(vcov, list):
        assert all(isinstance(v, str) for v in vcov), "vcov list must contain strings"
        assert all(
            v in data.columns for v in vcov
        ), "vcov list must contain columns in the data"
    if isinstance(vcov, str):
        assert vcov in [
            "iid",
            "hetero",
            "HC1",
            "HC2",
            "HC3",
        ], "vcov string must be iid, hetero, HC1, HC2, or HC3"


def _deparse_vcov_input(vcov: Union[str, dict[str, str]], has_fixef: bool, is_iv: bool):
    """
    Deparse the vcov argument passed to the Feols class.

    Parameters
    ----------
    vcov : Union[str, dict[str, str]]
        The vcov argument passed to the Feols class.
    has_fixef : bool
        Whether the regression has fixed effects.
    is_iv : bool
        Whether the regression is an IV regression.

    Returns
    -------
    vcov_type : str
        The type of vcov to be used. Either "iid", "hetero", or "CRV".
    vcov_type_detail : str or list
        The type of vcov to be used, with more detail. Options include "iid",
        "hetero", "HC1", "HC2", "HC3", "CRV1", or "CRV3".
    is_clustered : bool
        Indicates whether the vcov is clustered.
    clustervar : str
        The name of the cluster variable.
    """
    if isinstance(vcov, dict):
        vcov_type_detail = list(vcov.keys())[0]
        deparse_vcov = list(vcov.values())[0].split("+")
        if isinstance(deparse_vcov, str):
            deparse_vcov = [deparse_vcov]
        deparse_vcov = [x.replace(" ", "") for x in deparse_vcov]
    elif isinstance(vcov, (list, str)):
        vcov_type_detail = vcov
    else:
        assert False, "arg vcov needs to be a dict, string or list"

    if vcov_type_detail == "iid":
        vcov_type = "iid"
        is_clustered = False
    elif vcov_type_detail in ["hetero", "HC1", "HC2", "HC3"]:
        vcov_type = "hetero"
        is_clustered = False
        if vcov_type_detail in ["HC2", "HC3"]:
            if has_fixef:
                raise VcovTypeNotSupportedError(
                    "HC2 and HC3 inference types are not supported for regressions with fixed effects."
                )
            if is_iv:
                raise VcovTypeNotSupportedError(
                    "HC2 and HC3 inference types are not supported for IV regressions."
                )
    elif vcov_type_detail in ["CRV1", "CRV3"]:
        vcov_type = "CRV"
        is_clustered = True

    clustervar = deparse_vcov if is_clustered else None

    # loop over clustervar to change "^" to "_"
    if clustervar and "^" in clustervar:
        clustervar = [x.replace("^", "_") for x in clustervar]
        warnings.warn(
            f"""
            The '^' character in the cluster variable name is replaced by '_'.
            In consequence, the clustering variable(s) is (are) named {clustervar}.
            """
        )

    return vcov_type, vcov_type_detail, is_clustered, clustervar


def _apply_fixef_numpy(df_fe_values, fixef_dicts):
    fixef_mat = np.zeros_like(df_fe_values, dtype=float)
    for i, (fixef, subdict) in enumerate(fixef_dicts.items()):
        unique_levels, inverse = np.unique(df_fe_values[:, i], return_inverse=True)
        mapping = np.array([subdict.get(level, np.nan) for level in unique_levels])
        fixef_mat[:, i] = mapping[inverse]

    return fixef_mat
