# Equivariant Augmented Normalizing Flows

## Flows
- Perform equivariant shift (like in the NICE paper). See `bijector_nice.py`
- Perform projection and then RealNVP style scale and shift in projected space. See `bijector_proj_realnvp`.

### Tests
Currently implemented in 2D.

- See `distribution_test.py` where we test
(1) the distribution log prob is invariant to rotation and translation, and
(2) the bijector composed of multiple flow layers is equivariant to rotation and translation. 
- additionally `base_test.py` tests the base distribution and each bijector `.py` file has its own tests for equivariance. 


## Further notes
**need tricks for stability for the projected flow** 
- If we use zero init, then the flow is the identity transform which is fine.
- But for tests we don't use zero init, in which case we have to 
(1) use 64 bit, and 
(2) make the scale and shift very small to prevent massive changes to the initial points.



## TODO
 - Make proper SE(n) net. 
 - Test on simple target function (DW / LJ). 
 - Test jacobian determinant (test normalizing constant using IS?)
 - Think of how to make projected flow stable
 - For NICE flow we will need some scaling transforms. This could be done with a scaling layer applied to the base distribution. 