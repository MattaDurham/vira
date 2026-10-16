"""Modeled carp facial surfaces, including an articulated oral vestibule.

Jaw motion is informed by Gidmark et al.'s common-carp XROMM study:
https://xromm.org/projects/fish-feeding/ . This is an authored approximation,
not a specimen reconstruction. The respiratory cycle uses modest jaw motion,
not maximum feeding protrusion.
"""
import math

# X, circumferential angle, longitudinal/transverse radius, inlet elevation.
# The posterior opening is slightly larger and elongated along the head.
NARIAL_OPENINGS=((-0.590,.43,.0042,.0027,.0018),(-0.579,.48,.0046,.0029,.00035))


def rostral_profile(x):
    """Native collar radii and centre shared by its geometry and material chart."""
    u=max(0,min(1,(-x-.660)/.012))
    width=.0245-.0005*u
    radius=.025-.0175*u+.0003*math.sin(math.pi*u)
    center=-.03335-.00965*u
    return width,radius,radius,center


def opercular_span(t):
    return -.505,-.413+.039*math.sin(math.pi*t)**1.6


def opercular_coordinates(x,angle):
    side_angle=math.atan2(math.sin(angle),abs(math.cos(angle)))
    t=(side_angle+.95)/1.25
    if not 0<=t<=1:
        return t,-1
    front,rear=opercular_span(t)
    return t,(x-front)/(rear-front)


def author_face(mesh,curve,sphere,material,surface,skin_uv,ivory,cornea,pupil,register_barbel_root=None):
    from mathutils import Vector
    flesh=material("Facial mucosa",(.65,.59,.50),.44)
    flesh.node_tree.nodes["Principled BSDF"].inputs["Subsurface Weight"].default_value=.16
    flesh.node_tree.nodes["Principled BSDF"].inputs["IOR"].default_value=1.04
    flesh.node_tree.nodes["Principled BSDF"].inputs["Coat Weight"].default_value=.08
    inner=material("Oral mucosa",(.18,.063,.055),.46)
    dark=material("Recessed sensory canals",(.018,.020,.015),.54)
    branchial=material("Branchial mucosa",(.22,.085,.075),.58)
    branchial_bsdf=branchial.node_tree.nodes["Principled BSDF"]
    branchial_bsdf.inputs["IOR"].default_value=1.04
    branchial_bsdf.inputs["Coat Weight"].default_value=.02
    branchial_bsdf.inputs["Subsurface Weight"].default_value=.08
    # A continuous polar pigment field avoids six discrete spoke colours and
    # keeps the eye's microstructure independent of the body projection.
    import bpy
    from array import array
    iris_mat=material("Iris stromal pigment",(1,1,1),.27)
    iris_bsdf=iris_mat.node_tree.nodes["Principled BSDF"]
    # A subdued guanine-reflector approximation, brighter than dermis under
    # water. Reflecting fish irises are layered dielectrics, not metallic discs.
    iris_bsdf.inputs["IOR"].default_value=1.36
    iris_bsdf.inputs["Coat Weight"].default_value=.03
    width,height=1024,128
    image=bpy.data.images.new("Radial iris pigment",width=width,height=height,alpha=True)
    image.colorspace_settings.name="sRGB"
    pixels=array("f")
    for y in range(height):
        s=y/(height-1)
        outer=max(0,min(1,(s-.77)/.23));outer=outer*outer*(3-2*outer)
        for x in range(width):
            a=math.tau*x/(width-1)
            ring=.10+.025*math.sin(13*a)+.016*math.sin(31*a)
            collarette=.07*math.exp(-((s-ring)/.075)**2)
            warp=1.7*math.sin(a*7)+.7*math.sin(a*19+s*3)
            fibre=(.5+.5*math.sin(a*89+warp+s*4))**3
            branches=(.5+.5*math.sin(a*173+warp*1.8-s*11))**5
            fleck=max(0,math.sin(a*43+s*47+math.sin(a*11)*2))**18
            grain=.025*math.sin(a*397+s*181)*math.sin(a*251-s*113)
            shade=.87+.07*fibre+.045*branches-collarette-.09*fleck+grain
            silver=.28+.16*math.sin(5*a+s*4)+.09*math.sin(17*a-s*7)
            base=tuple((gold*(1-silver)+pearl*silver)*shade
                       for gold,pearl in zip((.74,.61,.36),(.74,.76,.68)))
            pixels.extend(tuple(c*(1-outer*.62)+edge*outer*.62
                                for c,edge in zip(base,(.12,.14,.10)))+(1,))
    image.pixels.foreach_set(pixels);image.pack()
    pigment=iris_mat.node_tree.nodes.new("ShaderNodeTexImage");pigment.image=image
    iris_mat.node_tree.links.new(pigment.outputs["Color"],iris_bsdf.inputs["Base Color"])

    def patch(name,rows,columns,point,mat,reverse=False):
        vertices=[point(i/rows,j/columns) for i in range(rows+1) for j in range(columns+1)]
        faces=[]
        for i in range(rows):
            for j in range(columns):
                a=i*(columns+1)+j;f=(a,a+1,a+columns+2,a+columns+1)
                faces.append(tuple(reversed(f)) if reverse else f)
        return mesh(name,vertices,faces,[mat])

    for sign in [-1,1]:
        # An orbital annulus blends into the skull, rather than adding a ball
        # on top of the face. The iris sits inside this fleshy socket.
        cx,cz=-.534,.0035
        width,upper,lower,center=_profile(surface,cx)
        eye_angle=math.asin(max(-.98,min(.98,(cz-center)/(upper if cz>=center else lower))))
        eye_base=abs(surface(cx,eye_angle).y)+.0018
        def socket(r,t):
            a=math.tau*t;radius=.018+.009*r
            x=cx+radius*math.cos(a);z=cz+radius*math.sin(a)
            _,upper,lower,center=_profile(surface,x)
            base=surface(x,math.asin(max(-.98,min(.98,(z-center)/max(upper if z>=center else lower,.001)))))
            y=abs(base.y)+.0012*(1-r)**2
            return (x,sign*y,z)
        orbital=patch("Orbital dermal annulus",12,128,socket,ivory,sign<0);skin_uv(orbital)
        # Radial iris crypts and corrugated stromal fibres are actual geometry.
        vertices,faces=[],[]
        rays,rings=192,20
        for i in range(rings+1):
            r=.0092+(.019-.0092)*i/rings
            for j in range(rays+1):
                k=j%rays
                a=math.tau*j/rays+.0016*math.sin(k*3.71)+.0011*math.sin(k*1.27)
                relief=.000018*math.sin(a*83+i*.7)+.000008*math.sin(a*137-i*.5)
                y=eye_base+.30*r*math.cos(a)+.0016*(1-(r/.022)**2)+relief
                vertices.append((cx+r*math.cos(a),sign*y,cz+r*math.sin(a)))
        for i in range(rings):
            for j in range(rays):
                a=i*(rays+1)+j;b=a+1
                f=(a,b,b+rays+1,a+rays+1);faces.append(f if sign>0 else tuple(reversed(f)))
        stroma=mesh("Iris radial stroma",vertices,faces,[iris_mat])
        uv=stroma.data.uv_layers.new(name="Radial iris pigment")
        for loop in stroma.data.loops:
            i,j=divmod(loop.vertex_index,rays+1);uv.data[loop.index].uv=(j/rays,i/rings)
        stroma["facial_joint"]="eye_L" if sign>0 else "eye_R"
        pupil_mesh=sphere("Recessed pupil",(cx,sign*(eye_base+.0012),cz),(.0093,.00065,.0093),pupil,segments=64)
        pupil_mesh.rotation_euler.z=sign*math.atan(.30)
        pupil_mesh["facial_joint"]=stroma["facial_joint"]
        # Cornea is a shallow transparent dome, not a thick glass bead.
        def dome(r,t):
            radius=.0195*r;a=math.tau*t
            return (cx+radius*math.cos(a),sign*(eye_base+.30*radius*math.cos(a)+.0032*math.sqrt(max(0,1-r*r))),cz+radius*math.sin(a))
        lens=patch("Cornea",32,128,dome,cornea,sign<0)
        lens["facial_joint"]=stroma["facial_joint"]

        # The head mesh has matching apertures. A raised anterior inlet and a
        # flatter posterior outlet blend into the actual curved head envelope.
        for x,angle,w,h,elevation in NARIAL_OPENINGS:
            theta=angle if sign>0 else math.pi-angle
            tangent=surface(x,theta+.0001)-surface(x,theta-.0001)
            along=surface(x+.0001,theta)-surface(x-.0001,theta)
            normal=tangent.cross(along).normalized()
            arc=h/(tangent.length/.0002)
            def footprint(r,t):
                a=math.tau*t
                return surface(x+w*r*math.cos(a),theta+arc*r*math.sin(a))
            def canal(r,t):
                return footprint(.82*r,t)+normal*(elevation-.0032*(1-r)**2)
            patch("Olfactory canal",16,96,canal,dark)
            collar=patch("Narial integrated collar",16,96,
                lambda u,t:footprint(.82+.56*u,t)+normal*(.0001+elevation*(1-u)**1.3),ivory)
            skin_uv(collar)
        theta=.455 if sign>0 else math.pi-.455
        normal=(surface(-.5845,theta+.0001)-surface(-.5845,theta-.0001)).cross(
            surface(-.5844,theta)-surface(-.5846,theta)).normalized()
        def crest(u,t):
            return surface(-.5845-.0018*u,theta+.055*(t-.5)*(1-.35*u),.0001)+normal*(.0023*u*math.sin(math.pi*t)**.75)
        flap=patch("Narial dividing crest",16,32,crest,ivory,sign<0);skin_uv(flap)

        # A bony cover replaces the removed head surface. Its underside and
        # free edge share vertices with the outer shell, rather than adding a
        # decorative tube over an otherwise uninterrupted cheek.
        def platepoint(t,u,inner_surface=False):
            angle=(-.95+1.25*t) if sign>0 else (math.pi+.95-1.25*t)
            front,rear=opercular_span(t)
            # The attached and dorsal/ventral margins share the head envelope.
            # Only the posterior free edge lifts; a uniform offset creates a
            # rectangular door seam around an otherwise curved gill cover.
            offset=.0018*math.sin(math.pi*u/2)**2*math.sin(math.pi*t)**1.6
            return surface(front+(rear-front)*u,angle,offset-(.0012 if inner_surface else 0))
        rows,columns=40,40
        count=(rows+1)*(columns+1)
        vertices=[platepoint(i/rows,j/columns,inside)
                  for inside in [False,True] for i in range(rows+1) for j in range(columns+1)]
        faces,indices=[],[]
        for i in range(rows):
            for j in range(columns):
                a=i*(columns+1)+j
                f=(a,a+1,a+columns+2,a+columns+1)
                if sign>0:
                    f=tuple(reversed(f))
                faces.extend([f,tuple(v+count for v in reversed(f))]);indices.extend([0,1])
        boundary=(list(range(columns+1))+
                  [i*(columns+1)+columns for i in range(1,rows+1)]+
                  [rows*(columns+1)+j for j in range(columns-1,-1,-1)]+
                  [i*(columns+1) for i in range(rows-1,0,-1)])
        if sign>0:
            boundary.reverse()
        for a,b in zip(boundary,boundary[1:]+boundary[:1]):
            faces.append((a,a+count,b+count,b));indices.append(0)
        plate=mesh("Opercular plate",vertices,faces,[ivory,branchial],indices)
        skin_uv(plate);plate["facial_joint"]=f"operculum_{'L' if sign>0 else 'R'}"
        # Periopercular attachment tissue remains continuous with the skull.
        # Keep the bony central cover rigid while a narrow overlap band flexes.
        weights=[]
        for inside in [False,True]:
            for i in range(rows+1):
                t=i/rows;band=min(1,min(t,1-t)/.06);band=band*band*(3-2*band)
                for j in range(columns+1):
                    x=platepoint(t,j/columns,inside).x
                    weights.append(max(0,min(1,(x+.505)/.012))*band)
        plate["facial_weights"]=weights
        # Do not average thin-shell side normals into the outer dermis. That
        # creates a bright rectangular rim even where the surfaces are flush.
        perimeter=set(boundary)
        for edge in plate.data.edges:
            a,b=edge.vertices
            if (a in perimeter and b in perimeter) or (a-count in perimeter and b-count in perimeter):
                edge.use_edge_sharp=True

        # The chamber is fixed to the skull. A recessed floor and surrounding
        # walls close the interior behind the real opening at every view angle.
        def chamberpoint(t,u,offset=-.011):
            angle=(-.95+1.25*t) if sign>0 else (math.pi+.95-1.25*t)
            front,rear=opercular_span(t)
            return surface(front+(rear-front)*u,angle,offset)
        chamber_vertices=[chamberpoint(i/rows,j/columns) for i in range(rows+1) for j in range(columns+1)]
        chamber_faces=[]
        for i in range(rows):
            for j in range(columns):
                a=i*(columns+1)+j;f=(a,a+1,a+columns+2,a+columns+1)
                chamber_faces.append(tuple(reversed(f)) if sign>0 else f)
        for index in boundary:
            i,j=divmod(index,columns+1)
            chamber_vertices.append(chamberpoint(i/rows,j/columns,-.0002))
        for i,a in enumerate(boundary):
            b=boundary[(i+1)%len(boundary)]
            chamber_faces.append((a,count+i,count+(i+1)%len(boundary),b))
        chamber=mesh("Branchial chamber",chamber_vertices,chamber_faces,[branchial]);chamber["rigid_joint"]=0

        # The ventral seal spans stationary chamber tissue and the moving
        # cover. Author its weights from that attachment, not cheek position.
        seal_rows,seal_columns=24,12
        def sealpoint(t,u):
            t=.02+.30*t
            return chamberpoint(t,.92,-.009).lerp(platepoint(t,1,True),u)
        seal=patch("Branchiostegal seal",seal_rows,seal_columns,sealpoint,branchial,sign<0)
        seal["facial_joint"]=plate["facial_joint"]
        seal["facial_weights"]=[(j/seal_columns)**2 for i in range(seal_rows+1) for j in range(seal_columns+1)]

    for lower in [False,True]:
        start=math.pi if lower else 0
        def collar(t,u):
            a=start+math.pi*t
            x=-.660-.012*u
            width,upper,lower_radius,center=rostral_profile(x)
            radius=upper if math.sin(a)>=0 else lower_radius
            return x,width*math.cos(a),center+radius*math.sin(a)
        rostrum=patch("Rostral lip attachment",64,24,collar,ivory,False)
        rostrum["facial_joint"]="jaw_lower" if lower else "jaw_upper"
        rostrum["material_chart"]="rostral"
        skin_uv(rostrum)

    # Thick lips are modeled around a continuous oral tube. No opaque nose cap
    # covers this opening; the dark cavity is behind an actual aperture.
    for lower in [False,True]:
        joint="jaw_lower" if lower else "jaw_upper"
        start=math.pi if lower else 0
        def lips(t,u):
            a=start+math.pi*t;b=math.tau*u
            thickness=.0035*(.55+.45*math.sin(math.pi*t))
            wrinkle=.00010*math.sin(a*37+b*3)
            r=thickness+wrinkle
            return (-.673-.65*r*math.cos(b),(.0215+r*math.sin(b))*math.cos(a),-.043+(.0035+r*math.sin(b))*math.sin(a))
        lip=patch("Lower labial fold" if lower else "Upper labial fold",96,24,lips,flesh,True)
        lip["facial_joint"]=joint
        def tube(t,u):
            a=start+math.pi*t
            w=.0195*(1-.64*u);h=.0025+.005*u
            return (-.673+.047*u,w*math.cos(a),-.043+h*math.sin(a))
        oral=patch("Lower buccal lining" if lower else "Upper buccal lining",96,24,tube,inner,lower)
        oral["facial_joint"]=joint
        for fold in range(3):
            # Fine longitudinal folds are sub-millimetre relief, not painted lines.
            points=[tube(i/48,.12+.19*fold) for i in range(49)]
            fold_obj=curve("Oral mucosal fold",points,.00038,inner);fold_obj["facial_joint"]=joint
    sphere("Buccal depth",(-.624,0,-.043),(.0025,.009,.006),dark,segments=48)

    # Two tapered sensory pairs. Cross-section shrinks to a soft rounded tip.
    for sign in [-1,1]:
        for small in [False,True]:
            origin=Vector((-.626,sign*.034,-.027)) if small else Vector((-.651,sign*.024,-.044))
            end=Vector((-.669,sign*.058,-.045)) if small else Vector((-.706,sign*.061,-.083))
            if register_barbel_root is not None:
                registered=Vector(register_barbel_root(origin.copy()))
                delta=registered-origin
                origin+=delta
                end+=delta
            vertices,faces=[],[]
            for i in range(49):
                t=i/48;point=origin.lerp(end,t)
                point.z+=.007*math.sin(math.pi*t);point.y+=sign*.008*math.sin(math.pi*t)
                radius=(.00135 if small else .0022)*(1-t)**.72+.00012
                for j in range(16):
                    a=math.tau*j/16;vertices.append(point+Vector((0,radius*math.cos(a),radius*math.sin(a))))
            for i in range(48):
                for j in range(16):
                    a=i*16+j;b=i*16+(j+1)%16;faces.append((a,b,b+16,a+16))
            barbel=mesh("Rostral sensory barbel" if small else "Maxillary sensory barbel",vertices,faces,[flesh])
            barbel["facial_joint"]="jaw_upper"


def _profile(surface,x):
    # Recover the envelope without coupling this module to the profile table.
    a=surface(x,0);b=surface(x,math.pi/2);c=surface(x,-math.pi/2)
    return abs(a.y),b.z-a.z,a.z-c.z,a.z
